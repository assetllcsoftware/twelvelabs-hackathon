# Architecture (current Terraform)

This is a snapshot of what `infra/*.tf` actually deploys today. Both the
synchronous read path (FastAPI portal on ECS) **and** the async
embedding + Pegasus + YOLO pipeline (EventBridge → 4 Lambdas + 3
Fargate workers) are now in Terraform; the slide deck
(`docs/energy-hackathon-deck.pptx`) draws each on its own slide. The
fourth Lambda + third worker (`start_yolo_task` + `yolo-detect-worker`)
ship their code under D.6 but only fire once the trained `.pt` files
are uploaded to `s3://.../models/yolo/<name>/v1/best.pt`.

## Resources Terraform owns today

### Synchronous read path — `infra/main.tf`, `infra/rds.tf`

| Layer | Resources |
| --- | --- |
| Network | `aws_vpc`, `aws_internet_gateway`, two `aws_subnet.public` (2 AZs), `aws_route_table` + association, `aws_security_group.alb`, `aws_security_group.app` |
| Edge | `aws_lb` (public ALB), `aws_lb_target_group`, `aws_lb_listener` (port 80) |
| Compute | `aws_ecs_cluster`, `aws_ecs_task_definition` (Fargate, 512 CPU / 1024 MiB), `aws_ecs_service` (1 task, public IP, ALB target) |
| Image registry | `aws_ecr_repository.app` |
| Logs | `aws_cloudwatch_log_group` (`/ecs/<project>`, 7-day retention) |
| Storage | `aws_s3_bucket.videos` (private, AES256, BucketOwnerEnforced, CORS) plus prefix markers: `raw-videos/`, `video-clips/`, `frames/`, `detections/`, `embeddings/videos/`, `embeddings/frames/`, `derived/clips/`, `models/yolo/` |
| Secrets | `aws_secretsmanager_secret.portal_token`, `aws_secretsmanager_secret.db` (with JSON `url` key) |
| Database | `aws_db_instance.postgres` (Postgres 16, `db.t4g.large`, gp3 20-50 GiB autoscale, single-AZ), tuned `aws_db_parameter_group` for pgvector / HNSW, `aws_db_subnet_group`, `aws_security_group.db` |
| IAM | `task_execution` role (ECR pull + Logs + read both secrets), `task` role (S3 list/CRUD on portal prefixes + `bedrock:InvokeModel` on `twelvelabs.marengo-embed-3-0-v1:0` and the `us.` cross-region inference profile) |

The ECS task receives `AWS_REGION`, `S3_BUCKET`, `PORTAL_CATEGORIES`, and
`RUN_MIGRATIONS=1` as plain env vars, plus `UPLOAD_PORTAL_TOKEN` and
`DATABASE_URL` (a libpq URL extracted from the `url` JSON key of the DB
secret) as ECS secrets resolved by the execution role.

### Async write path — `infra/embedding.tf`

| Layer | Resources |
| --- | --- |
| Eventing | `aws_s3_bucket_notification.videos_eventbridge` (bucket-level → EventBridge), `aws_cloudwatch_event_rule.video_uploaded` (prefix `raw-videos/`, three Lambda targets: clip / frame / yolo), `aws_cloudwatch_event_rule.clip_output_ready` (wildcard `embeddings/videos/*/output.json`, one target: finalize) plus matching `aws_lambda_permission` resources |
| Lambdas | `aws_lambda_function.start_clip_embed` (VPC, 256 MiB, calls `bedrock:StartAsyncInvoke`, upserts `videos`), `aws_lambda_function.finalize_clip_embed` (VPC, 512 MiB, L2-normalizes + INSERTs `kind='clip'`, then dispatches the Pegasus Fargate worker via `ecs:RunTask`), `aws_lambda_function.start_frame_task` (no VPC, 256 MiB, dispatches the Frame Fargate worker via `ecs:RunTask`), `aws_lambda_function.start_yolo_task` (no VPC, 256 MiB, dispatches the YOLO Fargate worker via `ecs:RunTask`) — all Python 3.12, pure-Python `pg8000` driver where DB access is needed, zips built by `scripts/build_lambdas.sh` |
| Fargate workers | `aws_ecs_task_definition.frame_embed_worker` (1024 CPU / 2048 MiB, 30 GiB ephemeral, ffmpeg + 8× parallel sync Marengo `InvokeModel`), `aws_ecs_task_definition.clip_pegasus_worker` (1024 CPU / 2048 MiB, 30 GiB ephemeral, ffmpeg cuts + sync Pegasus `InvokeModelWithResponseStream`), `aws_ecs_task_definition.yolo_detect_worker` (2048 CPU / 4096 MiB, 30 GiB ephemeral, CPU-only torch + ultralytics, downloads `models/yolo/<name>/v1/best.pt` and writes polygons to `frame_detections`), each with its own ECR repo, log group, and execution + task IAM roles |
| Networking | `aws_security_group.embedding_lambda` (VPC Lambdas), `aws_security_group.frame_worker`, `aws_security_group.clip_pegasus_worker`, `aws_security_group.yolo_detect_worker`, `aws_security_group_rule.db_ingress_from_*` (Postgres ingress for all four), `aws_security_group.vpc_endpoints` |
| VPC endpoints | `aws_vpc_endpoint.s3` (Gateway, free), `aws_vpc_endpoint.bedrock_runtime` (Interface, per-AZ ENI, private DNS), `aws_vpc_endpoint.secretsmanager` (Interface) — replaces the need for a NAT |

The Fargate worker containers live in `worker/frame_embed/` (Marengo
frame embeddings), `worker/clip_pegasus/` (Pegasus video-text), and
`worker/yolo_detect/` (YOLO instance segmentation). The lambda handlers
are in `lambda/{start_clip_embed,finalize_clip_embed,start_frame_task,start_yolo_task}/`.
`finalize_clip_embed` is the only lambda with `ecs:RunTask` on the
Pegasus task definition, so the Pegasus run only fires after Marengo's
clip rows are committed; the YOLO worker is launched on the same
`video_uploaded` event as the frame worker but self-gates by polling
`embeddings WHERE kind='frame'` until rows exist before running
inference.

## Diagram

```mermaid
flowchart LR
  user["Operator browser"]

  subgraph aws["AWS account (us-east-1)"]
    direction LR

    ECR[("ECR<br/>portal + 3 workers")]
    LOGS[/"CloudWatch Logs<br/>(portal · 4 Lambdas · 3 workers)"/]
    SEC_TOKEN[/"Secrets Manager<br/>portal shared token"/]
    SEC_DB[/"Secrets Manager<br/>RDS connection JSON"/]
    BR[["Bedrock Runtime<br/>Marengo Embed 3.0 + Pegasus 1.2"]]
    EB(("EventBridge bus"))
    L1["start_clip_embed (λ, VPC)"]
    L2["finalize_clip_embed (λ, VPC)"]
    L3["start_frame_task (λ, no VPC)"]
    L4["start_yolo_task (λ, no VPC)"]

    subgraph vpc["VPC 10.42.0.0/16"]
      direction LR

      subgraph pub["Two public subnets (2 AZs)"]
        ALB(["Public ALB :80"])
        ECS["ECS Fargate service<br/>FastAPI portal<br/>(1 task)"]
        WFRAME["frame-embed-worker<br/>(Fargate, on demand)"]
        WPEG["clip-pegasus-worker<br/>(Fargate, on demand)"]
        WYOLO["yolo-detect-worker<br/>(Fargate, on demand)"]
        RDS[("RDS Postgres 16<br/>db.t4g.large + pgvector")]
      end

      VPCE[/"VPC endpoints<br/>S3 (gw) · Bedrock (if) · Secrets (if)"/]
    end

    S3[("S3 bucket<br/>raw-videos/  video-clips/<br/>frames/  detections/<br/>embeddings/videos/<br/>embeddings/frames/<br/>derived/clips/<br/>models/yolo/")]
  end

  %% sync read path
  user -- "HTTPS<br/>shared-token cookie" --> ALB
  ALB -- "/health, /, /api/*" --> ECS
  user -. "presigned PUT/GET" .-> S3
  ECS -- "presigned PUT/GET" --> S3
  ECS -- "sync InvokeModel<br/>(query embedding)" --> BR
  ECS -- "cosine ANN<br/>(embedding <=> :q)<br/>JOIN clip_descriptions<br/>JOIN frame_detections" --> RDS
  ECS -- "GetSecretValue" --> SEC_TOKEN
  ECS -- "GetSecretValue" --> SEC_DB

  %% async write path
  S3 == "ObjectCreated<br/>→ bus" ==> EB
  EB == "video_uploaded rule" ==> L1
  EB == "video_uploaded rule" ==> L3
  EB == "video_uploaded rule" ==> L4
  L1 == "StartAsyncInvoke" ==> BR
  BR == "writes output.json" ==> S3
  EB == "clip_output_ready rule<br/>(wildcard=output.json)" ==> L2
  L2 == "INSERT kind='clip'" ==> RDS
  L2 == "ecs.run_task" ==> WPEG
  L3 == "ecs.run_task" ==> WFRAME
  L4 == "ecs.run_task" ==> WYOLO
  WFRAME == "ffmpeg → 8× InvokeModel<br/>S3 PUT thumbs" ==> S3
  WFRAME == "INSERT kind='frame'" ==> RDS
  WPEG == "ffmpeg cut → S3 PUT clip<br/>InvokeModelWithResponseStream" ==> BR
  WPEG == "INSERT clip_descriptions" ==> RDS
  WYOLO == "GET models/yolo/*<br/>GET embeddings/frames/*" ==> S3
  WYOLO == "INSERT frame_detections" ==> RDS

  classDef ext fill:#fef3c7,stroke:#92400e,color:#92400e;
  classDef storage fill:#dbeafe,stroke:#1e3a8a,color:#1e3a8a;
  classDef compute fill:#dcfce7,stroke:#14532d,color:#14532d;
  classDef secret fill:#fce7f3,stroke:#831843,color:#831843;
  classDef ai fill:#ede9fe,stroke:#5b21b6,color:#5b21b6;
  classDef event fill:#fef3c7,stroke:#b45309,color:#92400e;
  class user ext;
  class S3,RDS,ECR,LOGS storage;
  class ALB,ECS,WFRAME,WPEG,WYOLO,L1,L2,L3,L4 compute;
  class SEC_TOKEN,SEC_DB secret;
  class BR ai;
  class EB,VPCE event;
```

## Operator-visible flows

- **Search.** Browser → ALB → ECS portal. The portal embeds the query
  through Bedrock (sync `InvokeModel`), runs an HNSW cosine ANN on
  `embeddings`, snaps each clip hit to the highest-scoring frame inside
  its time window, dedupes within 3 s, joins `clip_descriptions` for
  pre-generated Pegasus text, joins `frame_detections` for YOLO
  polygons on the snapped frame, and returns presigned URLs (video
  with `#t=<sec>` fragment + frame thumbnail) plus the detections /
  Pegasus payloads inline. The UI overlays the polygons on the
  thumbnail with a master + per-class toggle.
- **Upload.** Browser → ECS for the presign → direct PUT to S3. The new
  object lands under `raw-videos/`, which fires the `video_uploaded`
  EventBridge rule. That rule fans out to **three** Lambdas:
  `start_clip_embed` (kicks off Bedrock async),
  `start_frame_task` (launches the frame-embed Fargate worker), and
  `start_yolo_task` (launches the yolo-detect Fargate worker). When
  Bedrock writes its `output.json`, the second rule
  (`clip_output_ready`) triggers `finalize_clip_embed` to land
  `kind='clip'` rows and dispatch the Pegasus Fargate worker; the
  frame worker writes `kind='frame'` rows directly via `psycopg`; the
  YOLO worker waits on those frame rows, then writes
  `frame_detections` rows.
- **YOLO models.** `s3://.../models/yolo/<name>/v1/best.pt` is the
  static input the YOLO worker reads. The roster (model name → S3
  key → class id/name/color) is a JSON env var on the task
  definition (`YOLO_MODELS`) so adding a third model is a one-line
  edit + `terraform apply`.

## What's still TODO

- DLQs / EventBridge alarm metrics on the four Lambdas + three Fargate
  workers.
- Filter search by detection class (`?has_class=pole`) — one extra
  `EXISTS` clause on `frame_detections` once the table is populated.
- GPU Fargate for the YOLO worker (single env-var swap to a GPU
  capacity provider when we outgrow CPU torch).
- Embedding the detections JSON so text-only search can find labelled
  events without an example image (V2).

## Slide deck

`docs/energy-hackathon-deck.pptx` walks through the same architecture
across three slides (sync read, async Marengo embed pipeline, async
Pegasus + YOLO enrichments), then dives into the clip-vs-frame story,
the frame-snap trick, the algorithm, and the roadmap. Regenerate with:

```bash
python3 docs/build_slides.py
```

This re-renders `docs/architecture/frame_snap.png` (matplotlib) and
writes a fresh `docs/energy-hackathon-deck.pptx`. Requires
`python-pptx` and `matplotlib` on the local interpreter (deliberately
*not* in `Pipfile` — the deployed image stays minimal).
