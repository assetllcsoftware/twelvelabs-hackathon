"""Generate clean standalone SVG renderings of the three architecture slides.

These mirror the python-pptx slides built by `docs/build_slides.py` but
strip the per-slide kicker / footer chrome and tighten the layout so the
diagrams stand on their own. The official AWS service PNGs in
`docs/icons/aws/` are embedded as base64 data URIs so the SVGs are fully
self-contained (no external file refs).

Outputs (next to the SVGs already in `docs/architecture/`):

  - architecture_sync.svg          --  synchronous read path (search portal)
  - architecture_marengo.svg       --  asynchronous Marengo embed pipeline
  - architecture_enrichments.svg   --  asynchronous Pegasus + YOLO pipelines

Run with::

    python3 docs/build_architecture_svgs.py
    python3 docs/render_svgs.py     # then refresh PNGs

"""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Iterable
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent
ARCH_DIR = ROOT / "architecture"
ICON_DIR = ROOT / "icons" / "aws"

ARCH_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Canvas + style
# ---------------------------------------------------------------------------
W, H = 1600, 940
BG = "#f8fafc"
INK = "#0f172a"
MUTED = "#475569"
SUBTLE = "#94a3b8"

ACCENT = "#1d4ed8"     # blue-700 -- main flow arrows
HIGHLIGHT = "#b45309"  # amber-700 -- secondary rule
DIM = "#94a3b8"        # gray for dashed back-links

CONTAINER_COLOR = {
    "cloud":  "#232f3e",  # AWS dark slate
    "region": "#00a4a6",  # AWS teal
    "vpc":    "#146eb4",  # AWS blue
    "subnet": "#7aa116",  # AWS green
}

# ---------------------------------------------------------------------------
# Icon loading -- embed PNGs as data URIs so the SVG is self-contained
# ---------------------------------------------------------------------------
ICON_FILES = {
    "alb":     "Arch_Elastic-Load-Balancing_64.png",
    "ecs":     "Arch_Amazon-Elastic-Container-Service_64.png",
    "fargate": "Arch_AWS-Fargate_64.png",
    "rds":     "Arch_Amazon-RDS_64.png",
    "s3":      "Arch_Amazon-Simple-Storage-Service_64.png",
    "bedrock": "Arch_Amazon-Bedrock_64.png",
    "ecr":     "Arch_Amazon-Elastic-Container-Registry_64.png",
    "events":  "Arch_Amazon-EventBridge_64.png",
    "lambda":  "Arch_AWS-Lambda_64.png",
    "secrets": "Arch_AWS-Secrets-Manager_64.png",
    "iam":     "Arch_AWS-Identity-and-Access-Management_64.png",
    "logs":    "Arch_Amazon-CloudWatch_64.png",
}

_ICON_CACHE: dict[str, str] = {}


def icon_uri(key: str) -> str:
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    data = base64.b64encode((ICON_DIR / ICON_FILES[key]).read_bytes()).decode("ascii")
    uri = f"data:image/png;base64,{data}"
    _ICON_CACHE[key] = uri
    return uri


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


def text(x: float, y: float, body: str, *, size: int = 12, weight: int = 400,
         fill: str = INK, anchor: str = "start",
         letter_spacing: float = 0) -> str:
    ls = f' letter-spacing="{letter_spacing}"' if letter_spacing else ""
    return (
        f'<text x="{x:.0f}" y="{y:.0f}" font-size="{size}" font-weight="{weight}" '
        f'fill="{fill}" text-anchor="{anchor}"{ls}>{escape(body)}</text>'
    )


def container(x: float, y: float, w: float, h: float, *, kind: str,
              label: str) -> str:
    color = CONTAINER_COLOR[kind]
    parts = [
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" '
        f'fill="white" stroke="{color}" stroke-width="1.5" '
        f'stroke-dasharray="6 4"/>'
    ]
    if label:
        tab_w = max(110, len(label) * 7 + 28)
        parts.append(
            f'<rect x="{x + 14}" y="{y - 12}" width="{tab_w}" height="22" '
            f'rx="4" fill="{color}"/>'
        )
        parts.append(text(x + 14 + tab_w / 2, y + 4, label,
                          size=11, weight=700, fill="white", anchor="middle"))
    return "\n".join(parts)


def service(cx: float, cy: float, icon_key: str, title: str,
            subtitle: str = "", *, icon_size: int = 56) -> str:
    """Bare AWS icon with a centered title + subtitle below.

    `cy` is the *icon center*. The whole node footprint is about
    icon_size + 36 px tall (icon + 2 lines of text)."""
    half = icon_size // 2
    parts = [
        f'<image href="{icon_uri(icon_key)}" '
        f'x="{cx - half:.0f}" y="{cy - half:.0f}" '
        f'width="{icon_size}" height="{icon_size}" '
        'preserveAspectRatio="xMidYMid meet"/>',
        text(cx, cy + half + 18, title, size=12, weight=700,
             fill=INK, anchor="middle"),
    ]
    if subtitle:
        parts.append(text(cx, cy + half + 34, subtitle, size=10,
                          fill=MUTED, anchor="middle"))
    return "\n".join(parts)


def operator_node(cx: float, cy: float) -> str:
    """Stylized 'person' node for the human user, matching AWS visual weight."""
    r = 14
    parts = [
        # head
        f'<circle cx="{cx}" cy="{cy - r - 4}" r="{r - 4}" '
        f'fill="white" stroke="{INK}" stroke-width="1.6"/>',
        # body
        f'<path d="M {cx - r} {cy + r} '
        f'a {r} {r} 0 0 1 {2 * r} 0 '
        f'L {cx + r} {cy + r + 8} '
        f'L {cx - r} {cy + r + 8} Z" '
        f'fill="white" stroke="{INK}" stroke-width="1.6"/>',
        text(cx, cy + r + 28, "Operator", size=12, weight=700,
             fill=INK, anchor="middle"),
        text(cx, cy + r + 44, "browser", size=10, fill=MUTED, anchor="middle"),
    ]
    return "\n".join(parts)


def arrow(x1: float, y1: float, x2: float, y2: float, *,
          color: str = ACCENT, width: float = 1.5,
          dashed: bool = False, curve: float = 0.0,
          label: str | None = None,
          label_t: float = 0.5,
          label_dx: float = 0, label_dy: float = -10,
          marker: str = "arrow") -> str:
    """Arrow from (x1, y1) to (x2, y2).

    `curve` of 0 draws a straight line. Positive `curve` bends the line
    upward (relative to the direction of travel); negative bends downward.

    `label_t` samples the line at a parametric position (0 = start, 1 = end).
    Use this to place labels off-center when two arrows would otherwise
    drop their midpoint labels on top of each other.
    """
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    cpx = cpy = None
    if curve == 0:
        line = (
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{color}" stroke-width="{width}"{dash} '
            f'marker-end="url(#{marker}_{color.lstrip("#")})"/>'
        )
    else:
        mx = (x1 + x2) / 2
        my = (y1 + y2) / 2
        dx = x2 - x1
        dy = y2 - y1
        length = (dx * dx + dy * dy) ** 0.5 or 1
        px = -dy / length
        py = dx / length
        cpx = mx + px * curve
        cpy = my + py * curve
        line = (
            f'<path d="M {x1:.1f} {y1:.1f} Q {cpx:.1f} {cpy:.1f} '
            f'{x2:.1f} {y2:.1f}" '
            f'fill="none" stroke="{color}" stroke-width="{width}"{dash} '
            f'marker-end="url(#{marker}_{color.lstrip("#")})"/>'
        )

    parts = [line]
    if label:
        t = label_t
        if cpx is None:
            lx = x1 + (x2 - x1) * t + label_dx
            ly = y1 + (y2 - y1) * t + label_dy
        else:
            it = 1 - t
            lx = it * it * x1 + 2 * it * t * cpx + t * t * x2 + label_dx
            ly = it * it * y1 + 2 * it * t * cpy + t * t * y2 + label_dy
        # White rounded background pill so the label reads against
        # crossing arrows / container borders.
        text_w = len(label) * 6.4 + 14
        parts.append(
            f'<rect x="{lx - text_w / 2:.0f}" y="{ly - 9:.0f}" '
            f'width="{text_w:.0f}" height="14" rx="4" '
            f'fill="white" fill-opacity="0.94"/>'
        )
        parts.append(text(lx, ly + 2, label, size=10, fill=MUTED,
                          anchor="middle"))
    return "\n".join(parts)


def title_block(title: str, subtitle: str) -> str:
    return "\n".join([
        text(40, 50, title, size=26, weight=800, fill=INK),
        text(40, 76, subtitle, size=13, fill=MUTED),
    ])


def footer_block(body: str, y: int = 905) -> str:
    return text(40, y, body, size=11, fill=MUTED)


# ---------------------------------------------------------------------------
# SVG wrapper
# ---------------------------------------------------------------------------


def svg_wrap(body: Iterable[str]) -> str:
    # We need a marker per arrow color. Build the union of colors used.
    colors = {ACCENT, HIGHLIGHT, DIM, INK}
    marker_defs: list[str] = []
    for c in colors:
        cid = "arrow_" + c.lstrip("#")
        marker_defs.append(
            f'<marker id="{cid}" markerWidth="9" markerHeight="9" '
            f'refX="8" refY="3" orient="auto" markerUnits="strokeWidth">'
            f'<path d="M0,0 L0,6 L8,3 z" fill="{c}"/></marker>'
        )
    return "\n".join([
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
        f'font-family="Inter, system-ui, -apple-system, \'Segoe UI\', '
        f'sans-serif">',
        '<defs>' + "".join(marker_defs) + '</defs>',
        f'<rect width="{W}" height="{H}" fill="{BG}"/>',
        *body,
        '</svg>',
    ])


# ---------------------------------------------------------------------------
# Diagram 1 -- Synchronous read path (search portal)
# ---------------------------------------------------------------------------


def build_sync() -> str:
    body: list[str] = []

    body.append(title_block(
        "Synchronous read path \u2014 search portal",
        "Operator browser \u2192 ALB \u2192 ECS Fargate FastAPI \u2192 "
        "RDS pgvector. Bedrock embeds the query inline."
    ))

    # Containers (drawn first so service icons sit on top)
    body.append(container(220, 140, 1360, 700, kind="cloud", label="AWS Cloud"))
    body.append(container(255, 185, 1295, 640, kind="region",
                          label="us-east-1"))
    body.append(container(290, 240, 460, 560, kind="vpc",
                          label="VPC \u00b7 10.42.0.0/16"))
    body.append(container(325, 305, 390, 470, kind="subnet",
                          label="Public Subnet AZ-a"))

    # Operator outside the cloud, on the left
    body.append(operator_node(110, 470))

    # In-VPC tiles, vertical stack
    ALB_CX, ALB_CY = 520, 380
    ECS_CX, ECS_CY = 520, 545
    RDS_CX, RDS_CY = 520, 710

    # Regional services in a vertical column on the right.
    SVC_CX = 1100
    S3_CY = 360
    BED_CY = 540
    SEC_CY = 720

    # --- Connectors (drawn before icons so any tail tucks under the icon) ---
    body.append(arrow(140, 470, ALB_CX - 32, ALB_CY + 6,
                      color=ACCENT, width=2.0,
                      label="HTTPS",
                      label_dy=-12))

    body.append(arrow(ALB_CX, ALB_CY + 32, ECS_CX, ECS_CY - 32,
                      color=ACCENT, width=2.0,
                      label="HTTP :8000",
                      label_dx=44, label_dy=4))

    body.append(arrow(ECS_CX, ECS_CY + 32, RDS_CX, RDS_CY - 32,
                      color=ACCENT, width=2.0,
                      label="cosine ANN",
                      label_dx=48, label_dy=4))

    # ECS to regional services -- fan out at three distinct y-targets so the
    # labels never collide.
    ecs_right_x = ECS_CX + 28
    body.append(arrow(ecs_right_x, ECS_CY - 18, SVC_CX - 28, S3_CY,
                      color=ACCENT, width=1.5,
                      label="presigned URLs",
                      label_dy=-10))
    body.append(arrow(ecs_right_x, ECS_CY, SVC_CX - 28, BED_CY,
                      color=ACCENT, width=1.5,
                      label="sync InvokeModel",
                      label_dy=-10))
    body.append(arrow(ecs_right_x, ECS_CY + 18, SVC_CX - 28, SEC_CY,
                      color=DIM, width=1.2, dashed=True,
                      label="GetSecretValue",
                      label_dy=14))

    # --- Service icons ---
    body.append(service(ALB_CX, ALB_CY, "alb",
                        "Application LB",
                        "HTTP :80 \u00b7 /healthz"))
    body.append(service(ECS_CX, ECS_CY, "ecs",
                        "ECS \u00b7 Fargate",
                        "FastAPI portal"))
    body.append(service(RDS_CX, RDS_CY, "rds",
                        "RDS \u00b7 pgvector",
                        "db.t4g.large \u00b7 HNSW"))
    body.append(service(SVC_CX, S3_CY, "s3",
                        "S3 bucket",
                        "raw-videos/ \u00b7 embeddings/"))
    body.append(service(SVC_CX, BED_CY, "bedrock",
                        "Bedrock",
                        "Marengo Embed 3.0"))
    body.append(service(SVC_CX, SEC_CY, "secrets",
                        "Secrets Manager",
                        "portal token \u00b7 DB URL"))

    body.append(footer_block(
        "ECR (image pulls), CloudWatch (logs) and IAM (task / execution "
        "roles) are also wired up but omitted here so the data path stays "
        "front and centre. The async ingest pipeline that fills the "
        "embeddings table sits on the next two diagrams."
    ))

    return svg_wrap(body)


# ---------------------------------------------------------------------------
# Diagram 2 -- Asynchronous Marengo embed pipeline
# ---------------------------------------------------------------------------


def build_marengo() -> str:
    body: list[str] = []

    body.append(title_block(
        "Asynchronous Marengo embed pipeline",
        "One S3 upload fans out to two parallel embed paths: a clip-level "
        "async Bedrock job and a frame-level Fargate worker."
    ))

    # Containers
    body.append(container(40, 140, 1540, 660, kind="cloud", label="AWS Cloud"))
    body.append(container(80, 185, 1500, 600, kind="region",
                          label="us-east-1"))
    body.append(container(260, 480, 1300, 290, kind="vpc", label="VPC"))

    # Top row (regional, no VPC) -- icon centers
    TOP_Y = 280
    S3_CX = 180
    EB_CX = 380
    SFT_CX = 600
    BED_CX = 1400

    # Bottom row (in VPC) -- icon centers
    BOT_Y = 610
    SCE_CX = 380
    FCE_CX = 620
    FW_CX = 940
    RDS_CX = 1400

    # --- Connectors (under icons) ---
    # S3 -> EventBridge (top, horizontal)
    body.append(arrow(S3_CX + 28, TOP_Y, EB_CX - 28, TOP_Y,
                      color=ACCENT, width=1.7,
                      label="ObjectCreated", label_dy=-12))

    # EB -> start_frame_task (top horizontal). Same rule covers the
    # diagonal EB -> start_clip_embed below, so we only label it once here.
    body.append(arrow(EB_CX + 28, TOP_Y, SFT_CX - 28, TOP_Y,
                      color=ACCENT, width=1.5,
                      label="video_uploaded",
                      label_dy=-12))

    # EB -> start_clip_embed (top -> bottom, no label - shares video_uploaded).
    body.append(arrow(EB_CX, TOP_Y + 30, SCE_CX, BOT_Y - 32,
                      color=ACCENT, width=1.5))

    # EB -> finalize_clip_embed (top -> bottom, separate rule)
    body.append(arrow(EB_CX + 22, TOP_Y + 22, FCE_CX - 22, BOT_Y - 22,
                      color=HIGHLIGHT, width=1.5,
                      label="clip_output_ready",
                      label_t=0.42, label_dx=8, label_dy=14))

    # start_frame_task -> frame-embed-worker (top -> bottom diagonal)
    body.append(arrow(SFT_CX + 22, TOP_Y + 22, FW_CX - 32, BOT_Y - 28,
                      color=ACCENT, width=1.4,
                      label="ecs.run_task",
                      label_t=0.62, label_dy=-12))

    # start_clip_embed -> Bedrock (bottom -> top, long diagonal).
    # Label sits early on the line, between start_clip_embed and the busy
    # middle of the diagram, so it never collides with 8x InvokeModel.
    body.append(arrow(SCE_CX + 28, BOT_Y - 12, BED_CX - 28, TOP_Y - 14,
                      color=ACCENT, width=1.3,
                      label="StartAsyncInvoke",
                      label_t=0.30, label_dy=-12))

    # frame-embed-worker -> Bedrock (bottom -> top, shorter)
    body.append(arrow(FW_CX + 28, BOT_Y - 18, BED_CX - 28, TOP_Y + 18,
                      color=ACCENT, width=1.3,
                      label="8\u00d7 InvokeModel",
                      label_t=0.55, label_dy=-12))

    # frame-embed-worker -> S3 (PUT thumbs, dashed back-link).
    # Route below the bottom-row icons, across to the left edge, and up to
    # S3 so it never cuts through any service tile.
    body.append(
        f'<path d="M {FW_CX - 28} {BOT_Y + 4} '
        f'V 730 '
        f'H {S3_CX} '
        f'V {TOP_Y + 30}" '
        f'fill="none" stroke="{DIM}" stroke-width="1.0" '
        f'stroke-dasharray="5 4" '
        f'marker-end="url(#arrow_{DIM.lstrip("#")})"/>'
    )
    body.append(text(S3_CX + 22, 724, "PUT frame thumbs",
                     size=10, fill=DIM))

    # finalize_clip_embed -> RDS (bottom row, INSERT clip). Routed slightly
    # above the row centerline so its label doesn't share a row with INSERT
    # frame.
    body.append(arrow(FCE_CX + 28, BOT_Y - 8, RDS_CX - 28, BOT_Y - 8,
                      color=ACCENT, width=1.5,
                      label="INSERT clip",
                      label_t=0.62, label_dy=-12))

    # frame-embed-worker -> RDS (bottom row, INSERT frame). Routed slightly
    # below the centerline.
    body.append(arrow(FW_CX + 28, BOT_Y + 12, RDS_CX - 28, BOT_Y + 12,
                      color=ACCENT, width=1.5,
                      label="INSERT frame",
                      label_dy=14))

    # --- Service icons ---
    body.append(service(S3_CX, TOP_Y, "s3",
                        "S3", "raw-videos/ \u00b7 output.json"))
    body.append(service(EB_CX, TOP_Y, "events",
                        "EventBridge", "2 rules"))
    body.append(service(SFT_CX, TOP_Y, "lambda",
                        "start_frame_task", "\u03bb \u00b7 dispatcher"))
    body.append(service(BED_CX, TOP_Y, "bedrock",
                        "Bedrock", "Marengo \u00b7 async + sync"))

    body.append(service(SCE_CX, BOT_Y, "lambda",
                        "start_clip_embed", "\u03bb in VPC"))
    body.append(service(FCE_CX, BOT_Y, "lambda",
                        "finalize_clip_embed", "L2-norm + INSERT"))
    body.append(service(FW_CX, BOT_Y, "fargate",
                        "frame-embed-worker",
                        "ffmpeg + 8\u00d7 Bedrock"))
    body.append(service(RDS_CX, BOT_Y, "rds",
                        "RDS \u00b7 pgvector",
                        "kind='clip' + kind='frame'"))

    body.append(footer_block(
        "Both Marengo branches share one bucket-level S3 \u2192 EventBridge "
        "notification. A third fan-out (start_yolo_task \u2192 yolo-detect-"
        "worker) plus the Pegasus enrichment fork live on the next diagram."
    ))

    return svg_wrap(body)


# ---------------------------------------------------------------------------
# Diagram 3 -- Asynchronous Pegasus + YOLO enrichments
# ---------------------------------------------------------------------------


def build_enrichments() -> str:
    body: list[str] = []

    body.append(title_block(
        "Asynchronous enrichments \u2014 Pegasus text + YOLO segmentation",
        "Two parallel post-processing pipelines hang off the same S3 events: "
        "Pegasus describes each clip in plain English; YOLO segments every "
        "frame thumb."
    ))

    # Containers
    body.append(container(40, 140, 1540, 660, kind="cloud", label="AWS Cloud"))
    body.append(container(80, 185, 1500, 600, kind="region",
                          label="us-east-1"))

    # Coordinates
    LEFT_CY = 470
    R1_CY = 280    # Pegasus row (top)
    R2_CY = 660    # YOLO row (bottom)

    S3_CX = 160
    EB_CX = 340
    L_CX = 580          # finalize_clip_embed / start_yolo_task
    W_CX = 860          # workers
    BED_CX = 1140
    RDS_CX = 1400
    RDS_CY = 470

    # --- Connectors ---
    # S3 -> EB (horizontal)
    body.append(arrow(S3_CX + 28, LEFT_CY, EB_CX - 28, LEFT_CY,
                      color=ACCENT, width=1.6,
                      label="ObjectCreated", label_dy=-12))

    # EB -> finalize_clip_embed (up-right diagonal)
    body.append(arrow(EB_CX + 22, LEFT_CY - 18, L_CX - 24, R1_CY + 22,
                      color=HIGHLIGHT, width=1.5,
                      label="clip_output_ready",
                      label_t=0.55, label_dx=20, label_dy=-12))

    # EB -> start_yolo_task (down-right diagonal)
    body.append(arrow(EB_CX + 22, LEFT_CY + 18, L_CX - 24, R2_CY - 22,
                      color=ACCENT, width=1.5,
                      label="video_uploaded",
                      label_t=0.6, label_dx=20, label_dy=14))

    # finalize_clip_embed -> clip-pegasus-worker (top row)
    body.append(arrow(L_CX + 28, R1_CY, W_CX - 28, R1_CY,
                      color=ACCENT, width=1.4,
                      label="ecs.run_task", label_dy=-12))

    # clip-pegasus-worker -> Bedrock
    body.append(arrow(W_CX + 28, R1_CY, BED_CX - 28, R1_CY,
                      color=ACCENT, width=1.4,
                      label="Pegasus stream", label_dy=-12))

    # clip-pegasus-worker -> RDS (long diagonal). Routed below the Pegasus
    # row so the labels don't crowd the Pegasus-stream label.
    body.append(arrow(W_CX + 16, R1_CY + 30, RDS_CX - 28, RDS_CY - 16,
                      color=ACCENT, width=1.4,
                      label="INSERT clip_descriptions",
                      label_t=0.6, label_dx=20, label_dy=-12))

    # start_yolo_task -> yolo-detect-worker (bottom row)
    body.append(arrow(L_CX + 28, R2_CY, W_CX - 28, R2_CY,
                      color=ACCENT, width=1.4,
                      label="ecs.run_task", label_dy=-12))

    # S3 -> yolo-detect-worker (dashed read of weights + frame thumbs).
    # Route below the YOLO row to keep the line out of the busy middle.
    body.append(
        f'<path d="M {S3_CX} {LEFT_CY + 30} '
        f'V 770 '
        f'H {W_CX} '
        f'V {R2_CY + 28}" '
        f'fill="none" stroke="{DIM}" stroke-width="1.0" '
        f'stroke-dasharray="5 4" '
        f'marker-end="url(#arrow_{DIM.lstrip("#")})"/>'
    )
    body.append(text(S3_CX + 22, 764,
                     "GET weights + frame thumbs",
                     size=10, fill=DIM))

    # yolo-detect-worker -> RDS (long diagonal up-right)
    body.append(arrow(W_CX + 16, R2_CY - 30, RDS_CX - 28, RDS_CY + 16,
                      color=ACCENT, width=1.4,
                      label="INSERT frame_detections",
                      label_t=0.6, label_dx=20, label_dy=14))

    # --- Service icons ---
    body.append(service(S3_CX, LEFT_CY, "s3", "S3",
                        "output.json \u00b7 YOLO weights"))
    body.append(service(EB_CX, LEFT_CY, "events", "EventBridge",
                        "clip_output_ready \u00b7 video_uploaded"))

    body.append(service(L_CX, R1_CY, "lambda", "finalize_clip_embed",
                        "\u03bb in VPC \u00b7 spawns Pegasus"))
    body.append(service(W_CX, R1_CY, "fargate", "clip-pegasus-worker",
                        "Fargate \u00b7 ffmpeg cuts \u2192 Pegasus"))
    body.append(service(BED_CX, R1_CY, "bedrock", "Bedrock \u00b7 Pegasus 1.2",
                        "invoke_model_with_response_stream"))

    body.append(service(L_CX, R2_CY, "lambda", "start_yolo_task",
                        "\u03bb \u00b7 ecs.run_task dispatcher"))
    body.append(service(W_CX, R2_CY, "fargate", "yolo-detect-worker",
                        "Fargate \u00b7 ultralytics CPU torch"))

    body.append(service(RDS_CX, RDS_CY, "rds", "RDS \u00b7 pgvector",
                        "clip_descriptions \u00b7 frame_detections"))

    body.append(footer_block(
        "Pegasus runs once Marengo's clip rows land (clip_output_ready). "
        "YOLO runs in parallel with the frame-embed worker on the same "
        "video_uploaded event. Both workers write to RDS and the search UI "
        "joins their rows at read time."
    ))

    return svg_wrap(body)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


DIAGRAMS = [
    ("architecture_sync.svg",         build_sync),
    ("architecture_marengo.svg",      build_marengo),
    ("architecture_enrichments.svg",  build_enrichments),
]


def main() -> None:
    for filename, builder in DIAGRAMS:
        out = ARCH_DIR / filename
        out.write_text(builder())
        rel = out.relative_to(ROOT.parent)
        print(f"wrote {rel}")


if __name__ == "__main__":
    main()
