"""Generate `docs/architecture/aws_resources.svg`.

A flat catalog of every AWS resource Terraform manages for the
``video-upload-portal`` stack, grouped by role (compute, network,
storage, IAM, ...).

Companion to ``build_pipelines_svg.py``: that one explains *what flows
through* the stack; this one enumerates *what exists*.

Run with::

    python3 docs/build_aws_resources_svg.py
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).resolve().parent / "architecture" / "aws_resources.svg"
OUT.parent.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

W, H = 1700, 1360

MARGIN_X = 45
COL_GAP = 30
CARD_W = (W - 2 * MARGIN_X - 3 * COL_GAP) // 4  # = 380
COLS_X = [MARGIN_X + i * (CARD_W + COL_GAP) for i in range(4)]

ROW1_Y, ROW1_H = 110, 290
ROW2_Y, ROW2_H = 420, 320
ROW3_Y, ROW3_H = 770, 290
ROW4_Y, ROW4_H = 1090, 230

LINE_HEIGHT = 17
HEADER_OFFSET = 24
BODY_TOP = 50  # first chip y, relative to card top


# ---------------------------------------------------------------------------
# Color presets
# ---------------------------------------------------------------------------

VIOLET = ("#7c3aed", "#f5f3ff")
INDIGO = ("#4338ca", "#eef2ff")
CYAN = ("#0e7490", "#ecfeff")
AMBER = ("#b45309", "#fefce8")
SKY = ("#1d4ed8", "#eff6ff")
RED = ("#b91c1c", "#fef2f2")
EMERALD = ("#047857", "#ecfdf5")
SLATE = ("#334155", "#f1f5f9")


# ---------------------------------------------------------------------------
# Card definitions — every Terraform-managed resource lives in one of these
# ---------------------------------------------------------------------------


@dataclass
class Card:
    title: str
    count: str
    color_pair: tuple[str, str]
    body: list[tuple[str, str]]  # (chip_text, sub_note); sub_note may be ""
    section: str = ""             # optional sub-heading line within the body


# Row 1 — compute
CARD_LAMBDA = Card(
    title="Lambda functions",
    count="4 funcs · 4 zips · 4 perms · 4 log groups",
    color_pair=VIOLET,
    body=[
        ("start_clip_embed",
         "VPC · Bedrock StartAsyncInvoke (Marengo) on raw-videos/*"),
        ("finalize_clip_embed",
         "VPC · upserts clip embeddings · ECS RunTask → Pegasus"),
        ("start_frame_task",
         "no-VPC · ECS RunTask → frame-embed-worker"),
        ("start_yolo_task",
         "no-VPC · ECS RunTask → yolo-detect-worker"),
        ("·  archive_file × 4",
         ".build/lambda/<name>.zip"),
        ("·  lambda_permission × 4",
         "events.amazonaws.com → InvokeFunction"),
    ],
)

CARD_ECS = Card(
    title="ECS / Fargate",
    count="1 cluster · 1 service · 4 task defs",
    color_pair=INDIGO,
    body=[
        ("ecs_cluster.this",
         "name: video-upload-portal"),
        ("ecs_service.app",
         "Fargate · desired_count=1 · behind ALB"),
        ("task_def: app",
         "portal FastAPI (Search + Upload UI)"),
        ("task_def: frame-embed-worker",
         "ffmpeg → Marengo image embed → DB"),
        ("task_def: clip-pegasus-worker",
         "ffmpeg-cut → Pegasus → clip_descriptions"),
        ("task_def: yolo-detect-worker",
         "ultralytics over frame thumbs → frame_detections"),
    ],
)

CARD_ECR = Card(
    title="ECR repositories",
    count="4 repos",
    color_pair=CYAN,
    body=[
        ("ecr_repository.app",
         "portal FastAPI image"),
        ("ecr_repository.frame_worker",
         "ffmpeg + boto3 (Marengo)"),
        ("ecr_repository.clip_pegasus_worker",
         "ffmpeg + psycopg + boto3 (Pegasus)"),
        ("ecr_repository.yolo_detect_worker",
         "ultralytics + torch image"),
    ],
)

CARD_EXTERNAL = Card(
    title="External — Bedrock + STS",
    count="3 services consumed",
    color_pair=AMBER,
    body=[
        ("Bedrock · Marengo embed-3",
         "us.twelvelabs.marengo-embed-3-0-v1:0"),
        ("Bedrock · Pegasus 1.2",
         "us.twelvelabs.pegasus-1-2-v1:0"),
        ("STS · GetCallerIdentity",
         "clip-pegasus-worker self-identifies for s3Location.bucketOwner"),
        ("(none of these are Terraform-provisioned)",
         "consumed via IAM policy + invoke API"),
    ],
)

# Row 2 — network / edge / events
CARD_VPC = Card(
    title="VPC + subnets + endpoints",
    count="1 VPC · 2 subnets · 3 endpoints",
    color_pair=SKY,
    body=[
        ("vpc.this",
         "10.0.0.0/16"),
        ("subnet.public[0]",
         "us-east-1a · 10.0.1.0/24"),
        ("subnet.public[1]",
         "us-east-1b · 10.0.2.0/24"),
        ("internet_gateway.this",
         "default route for 0.0.0.0/0"),
        ("route_table.public + 2 assocs",
         "shared by both public subnets"),
        ("vpc_endpoint.s3",
         "Gateway endpoint (free, route-table attached)"),
        ("vpc_endpoint.bedrock_runtime",
         "Interface endpoint (per-AZ ENI)"),
        ("vpc_endpoint.secretsmanager",
         "Interface endpoint (per-AZ ENI)"),
    ],
)

CARD_SECURITY = Card(
    title="Security groups + rules",
    count="8 SGs · 6 rules",
    color_pair=RED,
    body=[
        ("alb",
         "0.0.0.0/0 → :80"),
        ("app",
         "ALB → :8000 (FastAPI)"),
        ("db",
         "Postgres :5432 (ingress from 5 sources)"),
        ("vpc_endpoints",
         "VPC CIDR → :443"),
        ("embedding_lambda",
         "start_clip_embed + finalize_clip_embed ENIs"),
        ("frame_worker / clip_pegasus_worker / yolo_detect_worker",
         "one SG per Fargate task family"),
        ("rule: db_egress_all",
         "DB → outbound any"),
        ("rules: db_ingress_from_{app,embedding_lambda,frame,pegasus,yolo}",
         "5 separate ingress allow rules → DB :5432"),
    ],
)

CARD_EDGE = Card(
    title="Public ALB",
    count="1 LB · 1 listener · 1 target group",
    color_pair=INDIGO,
    body=[
        ("lb.app",
         "internet-facing ALB across both public subnets"),
        ("lb_listener.http",
         "HTTP :80 → forward → target group"),
        ("lb_target_group.app",
         "HTTP :8000 · health: GET /healthz"),
        ("DNS",
         "video-upload-portal-…elb.amazonaws.com"),
    ],
)

CARD_EVENTS = Card(
    title="EventBridge wiring",
    count="1 bus notif · 2 rules · 4 targets · 4 perms",
    color_pair=AMBER,
    body=[
        ("s3_bucket_notification.videos_eventbridge",
         "bucket → default event bus"),
        ("rule: video_uploaded",
         "raw-videos/* uploads"),
        ("  → start_clip_embed · start_frame_task · start_yolo_task",
         "three parallel pipelines"),
        ("rule: clip_output_ready",
         "embeddings/videos/*/output.json"),
        ("  → finalize_clip_embed",
         "Marengo async job has landed"),
        ("4 × lambda_permission",
         "events.amazonaws.com:InvokeFunction"),
        ("4 × event_target",
         "rule → lambda mappings"),
    ],
)

# Row 3 — data / observability
CARD_S3 = Card(
    title="S3 storage",
    count="1 bucket · 5 settings · 4 prefix objects",
    color_pair=EMERALD,
    body=[
        ("s3_bucket.videos",
         "video-upload-portal-14c0f02f"),
        ("cors / sse / public-access-block / ownership / notification",
         "5 sub-resources hardening the bucket"),
        ("prefix objects (managed)",
         "raw-videos/  video-clips/  frames/  detections/"),
        ("prefixes used by code (implicit)",
         "embeddings/videos/   embeddings/frames/"),
        ("",
         "derived/clips/        models/yolo/"),
        ("encryption",
         "AES256 SSE · public access fully blocked"),
    ],
)

CARD_RDS = Card(
    title="RDS Postgres",
    count="1 instance · 1 subnet group · 1 param group",
    color_pair=RED,
    body=[
        ("db_instance.postgres",
         "port 5432 · db: portal · pgvector + HNSW"),
        ("db_subnet_group.postgres",
         "spans both public subnets"),
        ("db_parameter_group.postgres",
         "tweaks for pgvector"),
        ("tables (created by /api/migrate)",
         "videos · embeddings · clip_descriptions · frame_detections"),
        ("driver",
         "psycopg in portal app · pg8000 in lambdas"),
    ],
)

CARD_SECRETS = Card(
    title="Secrets Manager",
    count="2 secrets · 2 versions · 3 randoms",
    color_pair=AMBER,
    body=[
        ("secret.db",
         "{host, port, dbname, username, password, url}"),
        ("secret_version.db",
         "JSON payload"),
        ("secret.portal_token",
         "shared bearer for the upload UI"),
        ("secret_version.portal_token",
         "string payload"),
        ("random_password.db / .portal_token",
         "Terraform-generated"),
        ("random_id.suffix",
         "8-char bucket name uniquifier"),
    ],
)

CARD_LOGS = Card(
    title="CloudWatch log groups",
    count="9 groups · retention 14d each",
    color_pair=SLATE,
    body=[
        ("/ecs/video-upload-portal",
         "portal FastAPI"),
        ("/ecs/…-frame-worker",
         "frame-embed-worker stdout"),
        ("/ecs/…-clip-pegasus-worker",
         "Pegasus worker (per-clip progress)"),
        ("/ecs/…-yolo-detect-worker",
         "YOLO predictions"),
        ("/aws/lambda/…-start-clip-embed",
         ""),
        ("/aws/lambda/…-finalize-clip-embed",
         ""),
        ("/aws/lambda/…-start-frame-task",
         ""),
        ("/aws/lambda/…-start-yolo-task",
         ""),
    ],
)

# Row 4 — wide IAM card spans all 4 columns
CARD_IAM = Card(
    title="IAM roles + policies",
    count="14 roles · 9 inline policies · 5 managed-policy attachments",
    color_pair=SLATE,
    body=[
        ("Lambda exec roles (4)",
         "trust: lambda.amazonaws.com"),
        ("  start_clip_embed",
         "AWSLambdaBasicExecution + VPCAccess + custom (Bedrock async, S3, secret)"),
        ("  finalize_clip_embed",
         "Basic + VPCAccess + custom (S3 read, secret, ECS RunTask, IAM PassRole)"),
        ("  start_frame_task",
         "Basic + custom (ECS RunTask + PassRole)"),
        ("  start_yolo_task",
         "Basic + custom (ECS RunTask + PassRole)"),
        ("ECS task roles (4 task + 4 execution)",
         "trust: ecs-tasks.amazonaws.com"),
        ("  task / task_execution",
         "portal app — Bedrock Marengo invoke + S3 + secret"),
        ("  frame_worker_task / _execution",
         "Bedrock Marengo image + S3 read/write + secret"),
        ("  clip_pegasus_worker_task / _execution",
         "Bedrock Pegasus invoke + invoke-stream + S3 + secret + STS"),
        ("  yolo_detect_worker_task / _execution",
         "S3 read frames + S3 read models + secret"),
    ],
)


CARDS_ROWS = [
    [CARD_LAMBDA, CARD_ECS, CARD_ECR, CARD_EXTERNAL],
    [CARD_VPC, CARD_SECURITY, CARD_EDGE, CARD_EVENTS],
    [CARD_S3, CARD_RDS, CARD_SECRETS, CARD_LOGS],
]
ROW_YH = [(ROW1_Y, ROW1_H), (ROW2_Y, ROW2_H), (ROW3_Y, ROW3_H)]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def text(x: float, y: float, body: str, *, size: int = 12, weight: int = 400,
         fill: str = "#0f172a", anchor: str = "start",
         letter_spacing: float = 0) -> str:
    ls = f' letter-spacing="{letter_spacing}"' if letter_spacing else ""
    return (
        f'<text x="{x:.0f}" y="{y:.0f}" font-size="{size}" font-weight="{weight}" '
        f'fill="{fill}" text-anchor="{anchor}"{ls}>{escape(body)}</text>'
    )


def card(x: int, y: int, w: int, h: int, c: Card) -> list[str]:
    color, tint = c.color_pair
    parts = [
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" '
        f'fill="{tint}" stroke="{color}" stroke-opacity="0.25" stroke-width="1"/>',
        f'<rect x="{x}" y="{y}" width="6" height="{h}" rx="2" fill="{color}"/>',
        text(x + 22, y + HEADER_OFFSET, c.title,
             size=14, weight=700, fill="#0f172a"),
        text(x + w - 14, y + HEADER_OFFSET, c.count,
             size=10, weight=600, fill=color, anchor="end"),
    ]
    cy = y + BODY_TOP
    for chip, note in c.body:
        if chip:
            parts.append(text(x + 22, cy, chip,
                              size=11, weight=600, fill="#0f172a"))
        if note:
            parts.append(text(x + 22, cy + 13, note,
                              size=10, weight=400, fill="#475569"))
            cy += LINE_HEIGHT + 13
        else:
            cy += LINE_HEIGHT
    return parts


def wide_card(x: int, y: int, w: int, h: int, c: Card) -> list[str]:
    """IAM card spans 4 columns; render the body as a 2-column list."""
    color, tint = c.color_pair
    parts = [
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" '
        f'fill="{tint}" stroke="{color}" stroke-opacity="0.25" stroke-width="1"/>',
        f'<rect x="{x}" y="{y}" width="6" height="{h}" rx="2" fill="{color}"/>',
        text(x + 22, y + HEADER_OFFSET, c.title,
             size=14, weight=700, fill="#0f172a"),
        text(x + w - 14, y + HEADER_OFFSET, c.count,
             size=10, weight=600, fill=color, anchor="end"),
    ]
    half = (len(c.body) + 1) // 2
    left = c.body[:half]
    right = c.body[half:]
    col_w = (w - 44) // 2
    for col_idx, items in enumerate((left, right)):
        cx = x + 22 + col_idx * col_w
        cy = y + BODY_TOP
        for chip, note in items:
            if chip:
                parts.append(text(cx, cy, chip,
                                  size=11, weight=600, fill="#0f172a"))
            if note:
                parts.append(text(cx, cy + 13, note,
                                  size=10, weight=400, fill="#475569"))
                cy += LINE_HEIGHT + 13
            else:
                cy += LINE_HEIGHT
    return parts


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build() -> str:
    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
        f'font-family="Inter, system-ui, -apple-system, \'Segoe UI\', sans-serif">'
    )
    parts.append(f'<rect width="{W}" height="{H}" fill="#f8fafc"/>')

    parts.append(text(MARGIN_X, 50, "AWS resource map",
                      size=30, weight=800, fill="#0f172a"))
    parts.append(text(
        MARGIN_X, 80,
        "Every resource Terraform manages for the energy-hackathon stack \u00b7 "
        "us-east-1 \u00b7 account 561700437092 \u00b7 project video-upload-portal "
        "\u00b7 112 resources.",
        size=13, fill="#64748b",
    ))

    for row_idx, row_cards in enumerate(CARDS_ROWS):
        ry, rh = ROW_YH[row_idx]
        for col_idx, c in enumerate(row_cards):
            cx = COLS_X[col_idx]
            parts.extend(card(cx, ry, CARD_W, rh, c))

    full_w = W - 2 * MARGIN_X
    parts.extend(wide_card(MARGIN_X, ROW4_Y, full_w, ROW4_H, CARD_IAM))

    parts.append(text(
        MARGIN_X, H - 30,
        "Lifecycle: S3 raw-videos/ upload \u2192 EventBridge fan-out \u2192 "
        "3 Lambdas \u2192 3 Fargate workers + Bedrock async \u2192 RDS "
        "(embeddings, clip_descriptions, frame_detections) \u2192 portal ALB "
        "renders results.",
        size=12, fill="#64748b",
    ))

    parts.append("</svg>")
    return "\n".join(parts)


def main() -> None:
    OUT.write_text(build())
    print(f"wrote {OUT.relative_to(OUT.parent.parent.parent)}")


if __name__ == "__main__":
    main()
