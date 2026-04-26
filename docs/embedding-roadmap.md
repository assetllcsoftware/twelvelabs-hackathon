# Embedding & Search Roadmap

End-to-end design for video search on the Energy Infrastructure Health
Platform. We use **TwelveLabs Marengo Embed 3.0** on Amazon Bedrock for the
multimodal model, **Postgres + `pgvector`** (an RDS instance we already
provisioned) for the vector store, and the **existing FastAPI portal**
(ECS Fargate behind an ALB) for the UI. New uploads are embedded
asynchronously by **Lambdas + EventBridge** so the portal stays responsive.

All of phases A through D.5 are shipped. Phase D.6 (YOLO instance
segmentation) has all code in tree but is gated on the trained
checkpoints from the sister `pld-yolo` project being uploaded to
`s3://.../models/yolo/...`. This file is now both the historical record
and the canonical reference for the schema, IAM, and local iteration.
The "what shipped" deltas vs. the original sketch are called out in
each section.

---

## Phase summary

| Phase | Where it runs | What changed | Status |
| --- | --- | --- | --- |
| **A** | laptop | `scripts/embed/` CLI: bulk-embed videos and frames, sync query embeddings, in-memory cosine search, presigned URLs with `#t=<timestamp_sec>` | ✅ shipped |
| **D.1** | AWS | `scripts/embed/migrations/0001_init.sql` + `0002_output_prefix.sql`; `app/db.py`; `DATABASE_URL` injected from Secrets Manager; `RUN_MIGRATIONS=1`; new `/api/db/health` | ✅ shipped |
| **D.2** | AWS | `/api/search/{text,image,text-image}` in `app/main.py` (mirrors `scripts/embed/serve.py`); Search tab in the portal HUD; task role gets `bedrock:InvokeModel` | ✅ shipped |
| **D.3** | AWS | clip pipeline: Lambdas `start_clip_embed` + `finalize_clip_embed` (Python 3.12, `pg8000`); EventBridge rules on `raw-videos/*` and `embeddings/videos/*/output.json`; IAM + VPC endpoints in `infra/embedding.tf` | ✅ shipped |
| **D.4** | AWS | frame pipeline: Lambda `start_frame_task` (no VPC) runs `ecs.run_task()` against a Fargate task `frame-embed-worker` (ffmpeg + 8× parallel sync `InvokeModel`, `psycopg[binary]`); writes `kind='frame'` rows + uploads thumbnails to `embeddings/frames/<id>/` | ✅ shipped |
| **D.5** | AWS | Pegasus pipeline: extra step in `finalize_clip_embed` Lambda runs `ecs.run_task()` against a Fargate task `clip-pegasus-worker` (ffmpeg cuts + sync Pegasus stream, `psycopg[binary]`); cuts uploaded to `derived/clips/<digest>/clip_*.mp4`; writes `clip_descriptions` rows; portal `/api/search/*` now joins them and the UI renders the cached text inline. Migration `0003_clip_descriptions.sql`. | ✅ shipped |
| **D.6** | AWS | YOLO instance-segmentation pipeline: third target on the existing `video_uploaded` rule fires `start_yolo_task` (no VPC) which dispatches a Fargate task `yolo-detect-worker` (CPU-only torch + ultralytics, `psycopg[binary]`). The worker waits for frame-embed rows, downloads `models/yolo/<name>/v1/best.pt` from S3 for each configured model, runs `model.predict()` against the existing thumbnails, converts masks to normalized polygons, and upserts `frame_detections`. Portal `/api/search/*` joins detections per (s3_key, frame_index) and the UI overlays per-class SVG polygons on each thumbnail with a master + per-class toggle. New endpoint `/api/detection-classes` powers the toggle bar. Migration `0004_frame_detections.sql`. | 🟡 code shipped, gated on uploaded weights |

Phase A was intentionally cheap: no infra changes, no notebook, no
Postgres. Phase D shipped as four small deploys (D.1 → D.4) so we could
stop, test, and demo at any boundary; if the clock had run out at D.3,
search still worked at clip granularity.

End-to-end smoke after D.4 (using `pipeline_vegetation001.mp4`):

```
$ curl -sH "x-upload-token: $TOKEN" .../api/db/health
{"status":"ok","postgres":"PostgreSQL 16.13 ...","pgvector":"0.8.1",
 "videos":1,"clips":13,"frames":80}
```

Search returns 3 frame-snapped hits with working presigned `thumb_url`
+ `presigned_url#t=<sec>` for the video.

---

## Architecture (as shipped — through Phase D.6)

The original sketch had two Lambdas. The shipped pipeline has **four**
Lambdas + **three** Fargate workers: the frame work landed in a Fargate
task (ffmpeg container, longer time budget, parallel `InvokeModel`),
Pegasus video-text generation landed in a second Fargate task that the
existing `finalize_clip_embed` Lambda dispatches once Marengo's clip
rows are written, and YOLO instance segmentation landed in a third
Fargate task dispatched in parallel with the frame worker. See
`docs/architecture.md` for the canonical Mermaid diagram drawn from
the Terraform; the high-level flow is:

```
raw-videos/<file>.mp4
  │  S3 ObjectCreated → EventBridge "video_uploaded" rule
  ├─▶ start_clip_embed   (λ, VPC) ─▶ bedrock.start_async_invoke
  │       └─▶ embeddings/videos/<job-uuid>/<bedrock-id>/output.json
  │             │  S3 ObjectCreated → "clip_output_ready" rule
  │             └─▶ finalize_clip_embed (λ, VPC)
  │                   ├─▶ INSERT kind='clip'
  │                   └─▶ ecs.run_task → clip-pegasus-worker
  │                         ├─▶ ffmpeg cut → derived/clips/<digest>/clip_*.mp4
  │                         ├─▶ bedrock.invoke_model_with_response_stream (Pegasus)
  │                         └─▶ INSERT clip_descriptions
  ├─▶ start_frame_task   (λ, no VPC) ─▶ ecs.run_task → frame-embed-worker
  │       └─▶ ffmpeg → 8× bedrock.invoke_model (parallel)
  │             ├─▶ S3 PUT thumbs under embeddings/frames/<id>/
  │             └─▶ INSERT kind='frame'
  └─▶ start_yolo_task    (λ, no VPC) ─▶ ecs.run_task → yolo-detect-worker
          ├─▶ poll embeddings until frame rows exist
          ├─▶ S3 GET models/yolo/<name>/v1/best.pt (per configured model)
          └─▶ ultralytics.YOLO.predict per thumb
                └─▶ INSERT frame_detections (polygons in [0,1])
```

Every arrow is one identity boundary the IAM policies have to cover.
See the **IAM** section below for the actual statements that shipped.

---

## Models and IDs

- **Foundation model id** (used by `start_async_invoke` for video):
  `twelvelabs.marengo-embed-3-0-v1:0`
- **Cross-region inference profile id** (used by `invoke_model` for sync
  text / image / text+image):
  - `us-east-1` → `us.twelvelabs.marengo-embed-3-0-v1:0`
  - `eu-west-1` → `eu.twelvelabs.marengo-embed-3-0-v1:0`
  - `ap-northeast-2` → `apac.twelvelabs.marengo-embed-3-0-v1:0`
- **Output dimensionality**: 512 (float32, L2-normalize before cosine).
- **Video segmentation**: Marengo returns one segment per ~6s clip with
  `startSec`, `endSec`, `embeddingOption`, `embeddingScope`, `embedding`.
- **Pegasus** (D.5) — video-to-text:
  - Foundation model id: `twelvelabs.pegasus-1-2-v1:0`
  - Inference profile id: `us.twelvelabs.pegasus-1-2-v1:0`
  - Called via `invoke_model_with_response_stream` on the cut clip
    uploaded to `derived/clips/<digest>/clip_<startms>_<endms>.mp4`.
- **YOLO-seg** (D.6) — instance segmentation. Trained checkpoints from
  the sister `pld-yolo` project:
  - `pldm-power-line/v1/best.pt` — 1 class: `power_line` (palette
    `#ff8c00`).
  - `airpelago-insulator-pole/v1/best.pt` — 2 classes: `insulator`
    (`#00e0ff`), `pole` (`#ff5cc6`).
  - The roster the Fargate task runs is plain JSON in the
    `YOLO_MODELS` env var (see `infra/embedding.variables.tf`); each
    entry is `{name, s3_key, version, classes:{id:name}, colors?:{id:hex}}`.
    Adding a third model is a one-row JSON edit + a re-`apply`.
  - Inference is CPU-only torch (no GPU Fargate yet) at `imgsz=640`,
    `conf=0.10`, `iou=0.5`. ~250–500 ms/frame; 80 frames × 2 models
    finishes in 1–2 min/video at hackathon scale.

---

## S3 layout

The portal provisions the user-facing prefixes (`raw-videos/`,
`video-clips/`, `frames/`, `detections/`). Embeddings live alongside
them under `embeddings/`:

```
s3://video-upload-portal-<suffix>/
  raw-videos/<file>.mp4                            # uploaded by users
  video-clips/<file>.mp4
  frames/<file>.{jpg,png}
  detections/<file>.{json,jsonl}
  embeddings/videos/<job-uuid>/<bedrock-id>/output.json   # Bedrock async
  embeddings/frames/<video-id>/frame_NNNNN.jpg            # frame-worker thumbs
  derived/clips/<video-id>/clip_<startms>_<endms>.mp4     # Pegasus cuts (D.5)
  models/yolo/<name>/v1/best.pt                           # YOLO weights (D.6)
```

`embeddings/videos/<job-uuid>/...` is what `start_async_invoke`'s
`s3OutputDataConfig` produces; `<job-uuid>` is generated by
`start_clip_embed` and stamped into `videos.output_prefix` (see migration
`0002_output_prefix.sql`). The finalize Lambda then resolves the parent
video row from the S3 key without parsing `invocationArn`s, which keeps
retries idempotent.

`embeddings/frames/<video-id>/` is the per-video thumbnail directory the
Fargate worker writes; the JPEGs are presigned by the portal at search
time (the portal task role got `s3:GetObject` on `embeddings/*` for
exactly this reason).

`derived/clips/<video-id>/clip_<startms>_<endms>.mp4` is the Pegasus
cut prefix. The clip-pegasus worker uploads each ffmpeg-cut window
here before invoking Pegasus on it via an `s3Location` media source.
The cuts are kept around (not deleted post-run) so re-runs with new
prompt presets don't have to re-cut.

---

## Postgres schema (Phase D.1)

`pgvector` is enabled on the RDS instance (parameter group tuned in
`infra/rds.tf`). The schema lives in `scripts/embed/migrations/` and is
baked into the portal image at `/app/migrations/`. The portal applies any
unrun migrations on startup when `RUN_MIGRATIONS=1` (recorded in a
`_migrations` table so re-runs are no-ops). Three migrations have
shipped:

- `0001_init.sql` — canonical DDL for `videos`, `embeddings`, and the
  HNSW + natural-key indexes below.
- `0002_output_prefix.sql` — adds `videos.output_prefix` so
  `finalize_clip_embed` can resolve the parent row from a Bedrock
  `output.json` ObjectCreated event without inspecting the
  `invocationArn`.
- `0003_clip_descriptions.sql` — adds the `clip_descriptions` table
  and its natural-key index for Pegasus video-text output (Phase D.5).
- `0004_frame_detections.sql` — adds the `frame_detections` table for
  YOLO-seg output (Phase D.6). Polygons stored normalized to `[0, 1]`
  so the UI can paint them over thumbnails of any size with one
  `<svg viewBox="0 0 1 1">`. Lookups go through
  `frame_detections_lookup_idx (s3_key, frame_index)`.

Clip and frame embeddings live in one table because they share an HNSW
index and the search path joins them anyway:

```sql
CREATE TABLE videos (
    s3_key text PRIMARY KEY,
    bucket text NOT NULL,
    bytes bigint,
    invocation_arn text,
    output_prefix text,                    -- added by 0002
    model_id text NOT NULL,
    status text NOT NULL DEFAULT 'pending',  -- pending | clips_ready | frames_ready | ready
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE embeddings (
    id bigserial PRIMARY KEY,
    s3_key text NOT NULL REFERENCES videos(s3_key) ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind IN ('clip','frame')),
    embedding_option text NOT NULL,        -- visual | audio | transcription | frame
    segment_index integer,                 -- non-null for kind='clip'
    frame_index integer,                   -- non-null for kind='frame'
    start_sec double precision NOT NULL,
    end_sec double precision NOT NULL,
    timestamp_sec double precision NOT NULL,
    thumb_s3_key text,                     -- non-null for kind='frame'
    embedding vector(512) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX embeddings_natural_key_idx ON embeddings
    (s3_key, kind, embedding_option,
     COALESCE(segment_index, -1), COALESCE(frame_index, -1));
CREATE INDEX embeddings_hnsw_cosine_idx ON embeddings
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Phase D.5
CREATE TABLE clip_descriptions (
    id bigserial PRIMARY KEY,
    s3_key text NOT NULL REFERENCES videos(s3_key) ON DELETE CASCADE,
    start_sec double precision NOT NULL,
    end_sec double precision NOT NULL,
    clip_s3_key text,                      -- derived/clips/<digest>/clip_*.mp4
    prompt_id text NOT NULL DEFAULT 'inspector',
    prompt text NOT NULL,
    message text NOT NULL,                 -- Pegasus output
    model_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX clip_descriptions_natural_key_idx
    ON clip_descriptions (s3_key, start_sec, end_sec, prompt_id);

-- Phase D.6
CREATE TABLE frame_detections (
    id            bigserial PRIMARY KEY,
    s3_key        text NOT NULL REFERENCES videos(s3_key) ON DELETE CASCADE,
    frame_index   integer NOT NULL,
    timestamp_sec double precision NOT NULL,
    thumb_s3_key  text NOT NULL,
    model_name    text NOT NULL,
    model_version text NOT NULL DEFAULT 'v1',
    class_id      integer NOT NULL,
    class_name    text NOT NULL,
    confidence    real NOT NULL,
    bbox_xyxy     real[] NOT NULL,           -- [x1,y1,x2,y2] in [0,1]
    polygon_xy    real[] NOT NULL,           -- flat [x0,y0,x1,y1,...] in [0,1]
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX frame_detections_lookup_idx
    ON frame_detections (s3_key, frame_index);
CREATE INDEX frame_detections_class_idx
    ON frame_detections (s3_key, class_name);
CREATE INDEX frame_detections_model_idx
    ON frame_detections (s3_key, model_name);
```

Search query (Phase D.2, parameterized — `:q` is a 512-element vector):

```sql
SELECT
    s3_key, kind, embedding_option, segment_index, frame_index,
    start_sec, end_sec, timestamp_sec, thumb_s3_key,
    1 - (embedding <=> :q) AS score
FROM embeddings
ORDER BY embedding <=> :q
LIMIT :pool;
```

The portal then runs `rank_results`-style refinement client-side: for each
clip in the result pool, snap `timestamp_sec` to the best frame whose
`timestamp_sec` falls inside the clip's `[start_sec, end_sec]` window, and
dedupe near-identical hits within ~3 seconds of the same `s3_key`.

Notes:
- We persist L2-normalized vectors so `<=>` (cosine distance) and the
  in-memory `matrix @ q` agree to floating point.
- The natural-key UNIQUE index is the upsert key for both Lambdas and the
  Fargate frame worker (`ON CONFLICT DO UPDATE`).

---

## IAM (as shipped)

The portal task **execution** role still owns ECR pull, CloudWatch Logs,
and `GetSecretValue` on the portal token + DB secret. The portal **task**
role is in `infra/main.tf` and now grants:

- S3 `list`/`get`/`put` on the portal prefixes (`raw-videos/`,
  `video-clips/`, `frames/`, `detections/`).
- `s3:GetObject` on `${bucket}/embeddings/*` so the search path can
  presign the frame thumbnails (PUT by the worker) and the Bedrock
  async output (PUT by Bedrock).
- `bedrock:InvokeModel` on the Marengo foundation model **and** the
  `us.` inference profile (sync query embeddings).
- `secretsmanager:GetSecretValue` on the DB secret.

The three Lambda roles live in `infra/embedding.tf`:

```hcl
# start_clip_embed (VPC) — Bedrock async kickoff + S3 + DB secret.
# StartAsyncInvoke implicitly checks bedrock:InvokeModel on the
# async-invoke/* resource, so we list both actions here.
data "aws_iam_policy_document" "lambda_start_clip_embed" {
  statement {
    sid     = "BedrockAsync"
    actions = ["bedrock:StartAsyncInvoke", "bedrock:GetAsyncInvoke", "bedrock:InvokeModel"]
    resources = concat(
      ["arn:aws:bedrock:*::foundation-model/${local.marengo_model_id}"],
      ["arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:async-invoke/*"],
    )
  }
  statement {
    sid       = "S3ReadInputWriteOutput"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.videos.arn, "${aws_s3_bucket.videos.arn}/*"]
  }
  statement {
    sid       = "ReadDbSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.db.arn]
  }
}

# finalize_clip_embed (VPC) — read S3 output.json + DB secret. No Bedrock.
data "aws_iam_policy_document" "lambda_finalize_clip_embed" {
  statement {
    sid       = "S3ReadOutput"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.videos.arn, "${aws_s3_bucket.videos.arn}/*"]
  }
  statement {
    sid       = "ReadDbSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.db.arn]
  }
}

# start_frame_task (no VPC) — only ECS RunTask + iam:PassRole.
data "aws_iam_policy_document" "lambda_start_frame_task" {
  statement {
    sid       = "RunWorkerTask"
    actions   = ["ecs:RunTask"]
    resources = [replace(aws_ecs_task_definition.frame_embed_worker.arn, "/:[0-9]+$/", ":*")]
  }
  statement {
    sid       = "PassWorkerRoles"
    actions   = ["iam:PassRole"]
    resources = [
      aws_iam_role.frame_worker_task.arn,
      aws_iam_role.frame_worker_execution.arn,
    ]
  }
}
```

The two VPC Lambdas attach the AWS-managed
`AWSLambdaVPCAccessExecutionRole` for ENI permissions instead of an
inline `VpcEni` statement.

The frame-worker **task** role mirrors `start_clip_embed` minus the
async actions: `bedrock:InvokeModel` (foundation model + `us.`
profile), full S3 read/write on the bucket (download the video, upload
thumbs), and `GetSecretValue` on the DB secret.

Both VPC Lambdas use the same `subnet_ids` as ECS plus a dedicated
security group; ingress to RDS comes from
`aws_security_group_rule.db_ingress_from_embedding_lambda` (and
`db_ingress_from_frame_worker` for the Fargate task).

### VPC endpoints (gotcha that bit us in deploy)

VPC-attached Lambdas in our public subnets do **not** get public IPs,
so once we pinned them to the VPC they couldn't reach Bedrock / Secrets
Manager / S3 over the internet. Rather than spin up a NAT gateway we
added three endpoints in `infra/embedding.tf`:

- `aws_vpc_endpoint.s3` — Gateway type, attached to the existing public
  route table (free).
- `aws_vpc_endpoint.bedrock_runtime` — Interface, both AZs, private DNS.
- `aws_vpc_endpoint.secretsmanager` — Interface, both AZs, private DNS.

The `vpc_endpoints` security group accepts 443 from the VPC CIDR. After
this change `start_clip_embed` consistently completes in ~1.5 s.

---

## EventBridge wiring (as shipped)

The bucket emits to EventBridge globally (`aws_s3_bucket_notification.videos_eventbridge`),
and two rules pull out the events we care about:

```hcl
# raw-videos/* uploads. Fan out to BOTH the clip kickoff and the frame
# Fargate dispatcher. We deliberately don't watch video-clips/ — that
# prefix is for downstream renders, not raw inputs.
resource "aws_cloudwatch_event_rule" "video_uploaded" {
  name = "${var.project_name}-video-uploaded"
  event_pattern = jsonencode({
    source        = ["aws.s3"]
    "detail-type" = ["Object Created"]
    detail = {
      bucket = { name = [aws_s3_bucket.videos.id] }
      object = { key = [{ prefix = "raw-videos/" }] }
    }
  })
}

# Bedrock async finished — output.json under embeddings/videos/<job-uuid>/.
# `wildcard` is required (vs. `suffix`) because the Bedrock-generated job id
# adds a path segment between our prefix and the filename.
resource "aws_cloudwatch_event_rule" "clip_output_ready" {
  name = "${var.project_name}-clip-output-ready"
  event_pattern = jsonencode({
    source        = ["aws.s3"]
    "detail-type" = ["Object Created"]
    detail = {
      bucket = { name = [aws_s3_bucket.videos.id] }
      object = { key = [{ wildcard = "embeddings/videos/*/output.json" }] }
    }
  })
}
```

The fan-out:

| Rule | Targets |
| --- | --- |
| `video_uploaded` | `start_clip_embed` λ + `start_frame_task` λ |
| `clip_output_ready` | `finalize_clip_embed` λ |

`start_clip_embed` calls `bedrock.start_async_invoke`, generates the
job-uuid prefix it'll write under, and upserts the `videos` row with
`invocation_arn` + `output_prefix` + `status='clips_pending'`.

`finalize_clip_embed` looks the row up by `output_prefix`, L2-normalizes
the embeddings, upserts all segments into `embeddings` with
`kind='clip'`, and flips `videos.status` to `clips_ready` (or `ready`
when frames are already in).

`start_frame_task` is intentionally not in the VPC: cold starts are
faster and it only needs `ecs:RunTask` + `iam:PassRole`. The Fargate
worker does the real work — ffmpeg → 8× parallel `bedrock.invoke_model`
→ S3 PUT + DB upsert (`psycopg[binary]`).

The two-Lambda split for the clip pipeline keeps the synchronous "kick
it off" hop under 5s. Bedrock's async invoke can take minutes; that
all happens off-Lambda.

---

## ECS task changes (Phase D — shipped)

The portal container in `infra/main.tf` got these env vars / secrets:

```hcl
{ name = "RUN_MIGRATIONS",        value = "1" },
{ name = "MARENGO_INFERENCE_ID",  value = "us.twelvelabs.marengo-embed-3-0-v1:0" },
# DATABASE_URL is injected as an ECS `secrets[]` entry pulled from
# the JSON `url` key of aws_secretsmanager_secret.db (handled by the
# task execution role).
```

The portal opens a `psycopg_pool.AsyncConnectionPool` on startup using
that URL. Migrations under `scripts/embed/migrations/` are bundled into
the image (see `app/Dockerfile`) and applied in order on first boot when
`RUN_MIGRATIONS=1`.

## Lambda packaging (gotcha that bit us in deploy)

Lambda zips are built by `scripts/build_lambdas.sh` into
`.build/lambda/<handler>/` and then zipped by Terraform's
`data "archive_file"` block. The script:

1. `pip install -r lambda/<handler>/requirements.txt -t
   .build/lambda/<handler>/`.
2. `cp lambda/<handler>/handler.py .build/lambda/<handler>/`.
3. Strips `__pycache__` and `*.egg-info` only.

We **do not** strip `*.dist-info` directories. `pg8000` depends on
`scramp`, which calls `importlib.metadata.version("scramp")` at import
time — without the dist-info, the Lambda fails with
`PackageNotFoundError: No package metadata was found for scramp` before
the handler can even load.

Run the script before every `terraform apply` that touches a Lambda;
Terraform fingerprints the build dir, so unchanged code is a no-op.

---

## Local-first runbook (today)

Repeat after every code change in Phase A:

```bash
# 1. credentials + bucket name
set -a; source ./.aws-demo.env; set +a
unset AWS_PROFILE
export AWS_CONFIG_FILE=/dev/null
export S3_BUCKET="$(terraform -chdir=infra output -raw bucket_name)"

# 2. embed everything new (cached on disk, idempotent)
pipenv run python -m scripts.embed.embed_videos

# 3. search
pipenv run python -m scripts.embed.search text "vegetation near a transmission line" -k 5
```

Smoke-test results from the first run (one video,
`raw-videos/pipeline_vegetation001.mp4`, 13 visual segments) confirmed the
top-K cluster around the visually-relevant segments and the presigned
`#t=<start_sec>` URLs jump straight to the matched moment in a browser.

---

## Phase D.6 — YOLO instance segmentation (code-complete, gated on weights)

Same dispatch shape as D.4 (frame embedding) — a no-VPC Lambda picks
up the same `video_uploaded` EventBridge rule and `ecs.run_task()`s a
Fargate worker:

- `lambda/start_yolo_task/handler.py` — line-for-line clone of
  `start_frame_task`, only the task definition and container name
  differ.
- `worker/yolo_detect/main.py` — boots, opens a single Postgres
  connection, polls `embeddings WHERE kind='frame'` until rows exist
  (default 15-minute deadline, 20 s poll), then for each model
  configured in `YOLO_MODELS`:
  1. Lazy-downloads `models/yolo/<name>/v1/best.pt` to ephemeral
     storage (cached across models in the same task).
  2. For each `embeddings.thumb_s3_key`, downloads the JPEG, runs
     `model.predict(retina_masks=True, imgsz=640, conf=0.10,
     iou=0.5)`, converts each instance mask to a normalized polygon
     via `cv2.findContours` + `cv2.approxPolyDP(eps=1.5px)`.
  3. `DELETE … WHERE (s3_key, frame_index, model_name) = (…)` then
     `INSERT` the new rows in one transaction per (frame, model) so
     re-runs are idempotent.
- The worker flips `videos.status` to `detections_ready` as a soft
  signal; the search API does **not** gate on it.

We deliberately re-use the frame-embed worker's thumbnails instead of
re-extracting frames so each search hit's `thumb_s3_key` has at most
one detection record per model — perfect 1:1 alignment with the
existing UI.

### Portal integration (Phase D.6)

`app/search.py` gained:

- `_fetch_detections_index(s3_keys, frame_indexes_by_key)` — pulls
  `frame_detections` rows for only the `(s3_key, frame_index)` pairs
  that survived refinement and dedupe. Falls back to `{}` when the
  table doesn't exist yet (fresh cluster pre-D.6) so search keeps
  working.
- `_attach_detections(...)` — attaches `r["detections"]` and
  `r["detection_classes"]` to each result.
- `detection_classes()` — public helper backing
  `GET /api/detection-classes`. Returns `{classes:[…], models:[…]}`
  with palette-assigned colors so the UI's chip strip is
  catalog-driven.

`app/static/app.js` learned to wrap every search-result thumbnail in
`<div class="search-thumb-wrap"><img …><svg viewBox="0 0 1 1">…</svg></div>`,
emit one `<polygon>` per detection (colored by class via a CSS custom
property), and toggle visibility with `data-detections="off"` on the
wrap and `data-class-on="false"` on individual polygons. The toggle
bar lives just below the corpus stats line in `index.html` and
updates polygons in-place — no re-render needed.

### Deploy runbook (D.6)

```bash
# 0. Make sure best.pt files exist under pld-yolo/runs/<run>/weights/.
#    The helper picks best.pt, falls back to last.pt automatically.
S3_BUCKET="$(terraform -chdir=infra output -raw bucket_name)" \
    bash scripts/upload_yolo_models.sh

# 1. Build everything (lambdas + portal + new worker image).
scripts/build_lambdas.sh                       # rebuilds start_yolo_task too

# 2. Build + push the new worker image to ECR (pattern matches D.4/D.5).
#    The repo is created by terraform apply, so apply once first if it's
#    not there yet, then re-apply after pushing.
terraform -chdir=infra apply

REGION=$(aws configure get region)
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REPO=$(terraform -chdir=infra output -raw yolo_detect_worker_repo 2>/dev/null \
       || aws ecr describe-repositories \
            --repository-names "$(terraform -chdir=infra output -raw project_name)-yolo-detect-worker" \
            --query 'repositories[0].repositoryUri' --output text)
aws ecr get-login-password --region "$REGION" | \
    docker login --username AWS --password-stdin "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
docker buildx build --platform linux/amd64 -f worker/yolo_detect/Dockerfile -t "$REPO:latest" --push .

# 3. Re-apply so the task definition picks up the new image digest.
terraform -chdir=infra apply

# 4. Force the portal to roll forward so 0004_frame_detections.sql runs
#    and the new /api/detection-classes endpoint ships.
aws ecs update-service --cluster "$(terraform -chdir=infra output -raw ecs_cluster_name)" \
    --service "$(terraform -chdir=infra output -raw portal_service_name)" \
    --force-new-deployment

# 5. Smoke: re-trigger the existing video so YOLO backfills it. The
#    cleanest way is to issue a no-op metadata-only copy:
S3_BUCKET="$(terraform -chdir=infra output -raw bucket_name)"
aws s3api copy-object --bucket "$S3_BUCKET" \
    --copy-source "$S3_BUCKET/raw-videos/pipeline_vegetation001.mp4" \
    --key "raw-videos/pipeline_vegetation001.mp4" \
    --metadata-directive REPLACE \
    --metadata "rerun=$(date -u +%Y%m%dT%H%M%SZ)"
```

If the YOLO worker errors before frame rows exist, it self-cancels
after `YOLO_WAIT_FOR_FRAMES_SEC` (default 900 s) — kick it manually
with `aws ecs run-task` once the frame worker finishes.

---

## Open follow-ups (out of scope for v1)

- **Detection embeddings.** Embed `detections/*.json` payloads
  (concatenate label text → sync `InvokeModel` text embedding, store as
  `kind='detection'`) so "find a thermal anomaly near a transformer"
  works even without a representative frame.
- **Filter search by detection class.** "Show me hits that contain a
  pole" is one extra `EXISTS` clause on `frame_detections` — easy
  follow-up now that the table exists.
- **GPU Fargate for YOLO.** CPU torch is fine at hackathon scale but
  saturates quickly. One Terraform variable swap to a GPU-backed
  capacity provider would cut per-frame inference 5-10×.
- **DLQ + retry.** Today a `Failed`/`Expired` Bedrock async job or a
  crashed Fargate worker is only logged. Wire SQS DLQs on the four
  Lambdas, an `aws_cloudwatch_event_rule` on
  `Bedrock Async Invocation Status Change` for `Failed`/`Expired`, and a
  retry knob on the Fargate tasks.
- **Auth on `/api/search/*`.** Inherited from the shared-token cookie
  middleware in `app/main.py` — works today but worth an explicit test.
- **Metrics.** The pipeline runs on logs only. CloudWatch metric
  filters for `start_clip_embed`/`finalize_clip_embed`/`start_frame_task`/
  `start_yolo_task` invocation count + error rate would make demo
  regressions visible before someone notices broken search results.
