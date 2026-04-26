"""Generate `docs/architecture/parallel_pipelines.svg`.

Marketing-grade dataflow picture: one inspection video, five parallel
pipelines, what the viewer gets at the end. Deliberately free of AWS-infra
noise *and* the engineering fine print — column-by-column you read what
happens, not how.

The two YOLO checkpoints (pldm-power-line, airpelago-insulator-pole) live
on separate lanes because they produce independent overlays.

Run with::

    python3 docs/build_pipelines_svg.py
"""
from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).resolve().parent / "architecture" / "parallel_pipelines.svg"
OUT.parent.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------
W, H = 1600, 860

SRC_X, SRC_Y, SRC_W, SRC_H = 40, 150, 200, 590

STAGE_W = 270
STAGE_W_LAST = 360
STAGE_H = 64
STAGES = [
    ("PREP", 280, STAGE_W),
    ("AI MODEL", 580, STAGE_W),
    ("INDEX", 880, STAGE_W),
    ("WHAT THE USER SEES", 1200, STAGE_W_LAST),
]

LANE_PITCH = 118
LANE_TOP_CENTER = 213  # y-center of the first lane

# ---------------------------------------------------------------------------
# Lanes
# ---------------------------------------------------------------------------
LANES = [
    {
        "name": "CLIP MATCH \u00b7 Marengo",
        "status": "live",
        "status_color": "#15803d",
        "color": "#4338ca",
        "tint": "#eef2ff",
        "stages": [
            "Hand the video to Bedrock",
            "Embed every ~6s clip",
            "Clip vectors",
            "Top matching clips per search",
        ],
    },
    {
        "name": "FRAME SNAP \u00b7 Marengo",
        "status": "live",
        "status_color": "#15803d",
        "color": "#0e7490",
        "tint": "#ecfeff",
        "stages": [
            "Sample 1 frame / second",
            "Embed every frame",
            "Frame vectors + thumbnails",
            "Jump to the right second",
        ],
    },
    {
        "name": "CLIP CAPTIONS \u00b7 Pegasus",
        "status": "live \u00b7 pipe demo",
        "status_color": "#b45309",
        "color": "#7c3aed",
        "tint": "#f5f3ff",
        "stages": [
            "Cut each clip out",
            "Describe it in plain English",
            "Caption per clip",
            "Caption under every result",
        ],
    },
    {
        "name": "POWER-LINE OVERLAY \u00b7 YOLO",
        "status": "awaiting weights",
        "status_color": "#b45309",
        "color": "#ea580c",
        "tint": "#fff7ed",
        "stages": [
            "Read the frame thumbs",
            "Find the power lines",
            "Power-line shapes",
            "Orange overlay on the frame",
        ],
    },
    {
        "name": "INSULATOR + POLE OVERLAY \u00b7 YOLO",
        "status": "awaiting weights",
        "status_color": "#b45309",
        "color": "#be185d",
        "tint": "#fdf2f8",
        "stages": [
            "Read the frame thumbs",
            "Find insulators + poles",
            "Insulator + pole shapes",
            "Cyan + pink overlay on the frame",
        ],
    },
]

assert len(LANES) == 5

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def lane_y(idx: int) -> int:
    return LANE_TOP_CENTER + idx * LANE_PITCH


def stage_y(idx: int) -> int:
    return lane_y(idx) - STAGE_H // 2


def text(x: float, y: float, body: str, *, size: int = 12, weight: int = 400,
         fill: str = "#0f172a", anchor: str = "start",
         letter_spacing: float = 0) -> str:
    ls = f' letter-spacing="{letter_spacing}"' if letter_spacing else ""
    return (
        f'<text x="{x:.0f}" y="{y:.0f}" font-size="{size}" font-weight="{weight}" '
        f'fill="{fill}" text-anchor="{anchor}"{ls}>{escape(body)}</text>'
    )


def stage_box(x: int, y: int, w: int, h: int, color: str, tint: str,
              body: str) -> str:
    parts = [
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" '
        f'fill="{tint}" stroke="{color}" stroke-opacity="0.25" stroke-width="1"/>',
        f'<rect x="{x}" y="{y}" width="6" height="{h}" rx="2" fill="{color}"/>',
        text(x + 20, y + h / 2 + 5, body, size=14, weight=600, fill="#0f172a"),
    ]
    return "\n".join(parts)


def lane_chip(x: int, y: int, name: str, color: str, status: str,
              status_color: str) -> str:
    chip_h = 22
    name_w = 16 + 6.4 * len(name)
    status_w = 14 + 6.0 * len(status)
    parts = [
        f'<rect x="{x}" y="{y}" width="{name_w:.0f}" height="{chip_h}" rx="11" '
        f'fill="{color}"/>',
        text(x + name_w / 2, y + 15, name, size=10, weight=700,
             fill="white", anchor="middle", letter_spacing=0.6),
        f'<rect x="{x + name_w + 8:.0f}" y="{y}" width="{status_w:.0f}" '
        f'height="{chip_h}" rx="11" fill="white" stroke="{status_color}" '
        f'stroke-width="1.4"/>',
        text(x + name_w + 8 + status_w / 2, y + 15, status, size=10, weight=700,
             fill=status_color, anchor="middle", letter_spacing=0.4),
    ]
    return "\n".join(parts)


def arrow(x1: float, y1: float, x2: float, y2: float, *,
          stroke: str = "#475569", width: float = 1.5,
          marker: str = "arrow") -> str:
    return (
        f'<line x1="{x1:.0f}" y1="{y1:.0f}" x2="{x2:.0f}" y2="{y2:.0f}" '
        f'stroke="{stroke}" stroke-width="{width}" '
        f'marker-end="url(#{marker})" fill="none"/>'
    )


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build() -> str:
    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
        f'font-family="Inter, system-ui, -apple-system, \'Segoe UI\', sans-serif">'
    )
    parts.append(
        '<defs>'
        '<marker id="arrow" markerWidth="10" markerHeight="10" '
        'refX="9" refY="3" orient="auto" markerUnits="strokeWidth">'
        '<path d="M0,0 L0,6 L9,3 z" fill="#475569"/>'
        '</marker>'
        '</defs>'
    )
    parts.append(f'<rect width="{W}" height="{H}" fill="#f8fafc"/>')

    parts.append(text(40, 56, "One video. Five pipelines in parallel.",
                      size=30, weight=800, fill="#0f172a"))
    parts.append(text(40, 84,
                      "Find the clip \u00b7 snap to the second \u00b7 "
                      "read the caption \u00b7 see what's there.",
                      size=14, fill="#64748b"))

    headers = [
        ("VIDEO", SRC_X + SRC_W // 2),
        ("PREP", STAGES[0][1] + STAGES[0][2] // 2),
        ("AI MODEL", STAGES[1][1] + STAGES[1][2] // 2),
        ("INDEX", STAGES[2][1] + STAGES[2][2] // 2),
        ("WHAT THE USER SEES", STAGES[3][1] + STAGES[3][2] // 2),
    ]
    for label, cx in headers:
        parts.append(text(cx, 124, label, size=10, weight=700,
                          fill="#94a3b8", anchor="middle", letter_spacing=2))

    src_cx = SRC_X + SRC_W // 2
    parts.append(
        f'<rect x="{SRC_X}" y="{SRC_Y}" width="{SRC_W}" height="{SRC_H}" '
        f'rx="14" fill="#0f172a"/>'
    )
    parts.append(text(src_cx, SRC_Y + SRC_H / 2 + 6,
                      "Inspection video",
                      size=20, weight=700, fill="white", anchor="middle"))

    for i, lane in enumerate(LANES):
        ly = lane_y(i)
        sy = stage_y(i)
        color = lane["color"]
        tint = lane["tint"]

        parts.append(arrow(SRC_X + SRC_W, ly, STAGES[0][1] - 4, ly,
                           stroke=color, width=2.2))

        parts.append(
            lane_chip(STAGES[0][1] + 12, sy - 30,
                      lane["name"], color,
                      lane["status"], lane["status_color"])
        )

        for j, (sx, sw) in enumerate([(s[1], s[2]) for s in STAGES]):
            parts.append(stage_box(sx, sy, sw, STAGE_H, color, tint,
                                   lane["stages"][j]))

        for j in range(3):
            x_from = STAGES[j][1] + STAGES[j][2]
            x_to = STAGES[j + 1][1]
            parts.append(arrow(x_from, ly, x_to - 4, ly,
                               stroke="#94a3b8", width=1.4))

    parts.append("</svg>")
    return "\n".join(parts)


def main() -> None:
    OUT.write_text(build())
    print(f"wrote {OUT.relative_to(OUT.parent.parent.parent)}")


if __name__ == "__main__":
    main()
