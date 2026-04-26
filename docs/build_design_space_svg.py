"""Generate `docs/architecture/design_space.svg`.

A *theoretical* flow chart: every plausible way an inspection video can move
through this stack. Six columns wide --

    INPUT \u2192 FEED IT AS \u2192 PRE-PROCESS \u2192 AI MODEL \u2192 POST-PROCESS \u2192 INDEX

-- with one node per option in each column. The five shipped pipelines are
drawn as bold colored lanes through the DAG; the un-tried branches sit in
their columns with dashed borders and faint connectors so you can see the
dials we *could* turn next without committing to any of them.

Run with::

    python3 docs/build_design_space_svg.py
"""
from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).resolve().parent / "architecture" / "design_space.svg"
OUT.parent.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------
W, H = 1640, 1100
LANE_CENTER_Y = 610
NODE_H = 44
NODE_PITCH = 60  # 44 + 16

COLUMNS = [
    # key,    header text,            x,    width, accent color
    ("input", "VIDEO",                40,   160,   "#0f172a"),
    ("feed",  "FEED IT AS",           260,  170,   "#1d4ed8"),
    ("pp",    "PRE-PROCESS",          480,  240,   "#b45309"),
    ("model", "AI MODEL",             780,  220,   "#7c3aed"),
    ("post",  "POST-PROCESS",         1060, 240,   "#0f766e"),
    ("idx",   "INDEX",                1340, 260,   "#be185d"),
]
COL = {k: dict(zip(("key", "header", "x", "w", "color"), c)) for k, *_ in
       [(c[0],) for c in COLUMNS] for c in [next(cc for cc in COLUMNS if cc[0] == k)]}


def col(key: str) -> dict:
    return next(c for c in (
        {"key": k, "header": h, "x": x, "w": w, "color": color}
        for (k, h, x, w, color) in COLUMNS
    ) if c["key"] == key)


# ---------------------------------------------------------------------------
# Nodes per column
# ---------------------------------------------------------------------------
# Each node: id (column-local), label, status: "live" | "demo" | "gated" | "explore"

NODES: dict[str, list[tuple[str, str, str]]] = {
    "input": [
        ("video", "Inspection video", "live"),
    ],
    "feed": [
        ("as_video",  "as video",  "live"),
        ("as_clip",   "as clip",   "live"),
        ("as_frame",  "as frame",  "live"),
    ],
    "pp": [
        ("none",       "passthrough",                    "live"),
        ("cut6s",      "ffmpeg cut \u2192 ~6 s clips",   "live"),
        ("sample1fps", "ffmpeg sample \u2192 1 fps",     "live"),
        ("scenedet",   "scene-detect re-cut",            "explore"),
        ("keyframe",   "key-frame select",               "explore"),
        ("roi",        "ROI / object crop",              "explore"),
        ("bgmask",     "background mask",                "explore"),
        ("stabilize",  "stabilize",                      "explore"),
        ("superres",   "super-resolution",               "explore"),
        ("overlay",    "polygon overlay",                "explore"),
    ],
    "model": [
        ("marengo_v",  "Marengo \u00b7 video mode",      "live"),
        ("marengo_i",  "Marengo \u00b7 image mode",      "live"),
        ("pegasus",    "Pegasus 1.2 \u00b7 describe",    "demo"),
        ("yolo_pl",    "YOLO \u00b7 power-line",         "gated"),
        ("yolo_ip",    "YOLO \u00b7 insulator + pole",   "gated"),
    ],
    "post": [
        ("l2",        "L2 normalize",                "live"),
        ("framesnap", "frame-snap to second",        "live"),
        ("dedupe",    "per-source dedupe",           "live"),
        ("topk",      "top-K + threshold",           "live"),
        ("masktopoly","mask \u2192 polygon",         "live"),
        ("rrf",       "reciprocal rank fusion",      "explore"),
        ("captjoin",  "caption re-embed \u2192 join","explore"),
        ("cluster",   "cluster / classify",          "explore"),
    ],
    "idx": [
        ("vec_clip",  "Vector index \u00b7 clips",   "live"),
        ("vec_frame", "Vector index \u00b7 frames",  "live"),
        ("captions",  "Caption table",               "live"),
        ("polygons",  "Polygon table",               "live"),
        ("cluster_i", "Cluster / class index",       "explore"),
    ],
}

# ---------------------------------------------------------------------------
# Status palette
# ---------------------------------------------------------------------------
STATUS_FILL = {
    "live":    "#ecfdf5",   # green-50
    "demo":    "#fef3c7",   # amber-50
    "gated":   "#fef3c7",
    "explore": "#ffffff",
}
STATUS_BORDER = {
    "live":    "#15803d",
    "demo":    "#b45309",
    "gated":   "#b45309",
    "explore": "#94a3b8",
}
STATUS_TEXT = {
    "live":    "#065f46",
    "demo":    "#92400e",
    "gated":   "#92400e",
    "explore": "#475569",
}
STATUS_LABEL = {
    "live":    "live",
    "demo":    "live \u00b7 pipe demo",
    "gated":   "awaiting weights",
    "explore": "explore",
}

# ---------------------------------------------------------------------------
# Shipped pipelines (the five bold colored lanes)
# ---------------------------------------------------------------------------
# Each lane: name, color, ordered list of (column_key, node_id) hops.

LANES = [
    {
        "name": "CLIP MATCH",
        "color": "#4338ca",
        "hops": [
            ("input", "video"),
            ("feed",  "as_video"),
            ("pp",    "none"),
            ("model", "marengo_v"),
            ("post",  "l2"),
            ("idx",   "vec_clip"),
        ],
    },
    {
        "name": "FRAME SNAP",
        "color": "#0e7490",
        "hops": [
            ("input", "video"),
            ("feed",  "as_frame"),
            ("pp",    "sample1fps"),
            ("model", "marengo_i"),
            ("post",  "framesnap"),
            ("idx",   "vec_frame"),
        ],
    },
    {
        "name": "CLIP CAPTIONS",
        "color": "#7c3aed",
        "hops": [
            ("input", "video"),
            ("feed",  "as_clip"),
            ("pp",    "cut6s"),
            ("model", "pegasus"),
            ("post",  "captjoin"),
            ("idx",   "captions"),
        ],
    },
    {
        "name": "POWER-LINE OVERLAY",
        "color": "#ea580c",
        "hops": [
            ("input", "video"),
            ("feed",  "as_frame"),
            ("pp",    "sample1fps"),
            ("model", "yolo_pl"),
            ("post",  "masktopoly"),
            ("idx",   "polygons"),
        ],
    },
    {
        "name": "INSULATOR + POLE",
        "color": "#be185d",
        "hops": [
            ("input", "video"),
            ("feed",  "as_frame"),
            ("pp",    "sample1fps"),
            ("model", "yolo_ip"),
            ("post",  "masktopoly"),
            ("idx",   "polygons"),
        ],
    },
]

# Mark caption-join as live for the visual since the Pegasus lane uses it
# (the "explore" tag in the catalog is for *new* fusion patterns).
NODES["post"][6] = ("captjoin", "caption re-embed \u2192 join", "live")

# ---------------------------------------------------------------------------
# Exploratory edges -- thin dashed gray hints that show the design space
# ---------------------------------------------------------------------------
# (from_col, from_id, to_col, to_id)

EXPLORE_EDGES: list[tuple[str, str, str, str]] = [
    # FEED -> PP (alternates)
    ("feed", "as_video",  "pp", "stabilize"),
    ("feed", "as_video",  "pp", "roi"),
    ("feed", "as_clip",   "pp", "scenedet"),
    ("feed", "as_clip",   "pp", "stabilize"),
    ("feed", "as_clip",   "pp", "overlay"),
    ("feed", "as_frame",  "pp", "keyframe"),
    ("feed", "as_frame",  "pp", "roi"),
    ("feed", "as_frame",  "pp", "bgmask"),
    ("feed", "as_frame",  "pp", "superres"),
    ("feed", "as_frame",  "pp", "overlay"),

    # PP (explore) -> MODEL
    ("pp", "scenedet",  "model", "marengo_v"),
    ("pp", "scenedet",  "model", "pegasus"),
    ("pp", "keyframe",  "model", "marengo_i"),
    ("pp", "roi",       "model", "marengo_v"),
    ("pp", "roi",       "model", "marengo_i"),
    ("pp", "bgmask",    "model", "marengo_i"),
    ("pp", "stabilize", "model", "marengo_v"),
    ("pp", "superres",  "model", "marengo_i"),
    ("pp", "overlay",   "model", "marengo_i"),
    ("pp", "overlay",   "model", "pegasus"),

    # MODEL -> POST (alt post-procs)
    ("model", "marengo_v", "post", "rrf"),
    ("model", "marengo_i", "post", "rrf"),
    ("model", "marengo_v", "post", "cluster"),
    ("model", "marengo_i", "post", "cluster"),
    ("model", "pegasus",   "post", "cluster"),
    ("model", "yolo_pl",   "post", "cluster"),
    ("model", "yolo_ip",   "post", "cluster"),

    # POST -> INDEX (cluster)
    ("post", "cluster", "idx", "cluster_i"),
    ("post", "rrf",     "idx", "vec_clip"),
    ("post", "rrf",     "idx", "vec_frame"),
]

# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------


def column_positions() -> dict[str, dict[str, tuple[int, int, int, int]]]:
    """Return {col_key: {node_id: (x, y, w, h)}}."""
    out: dict[str, dict[str, tuple[int, int, int, int]]] = {}
    for c in COLUMNS:
        key, _, x, w, _ = c
        nodes = NODES[key]
        n = len(nodes)
        top_y = LANE_CENTER_Y - (n * NODE_PITCH - (NODE_PITCH - NODE_H)) // 2
        out[key] = {}
        for i, (nid, _label, _status) in enumerate(nodes):
            out[key][nid] = (x, top_y + i * NODE_PITCH, w, NODE_H)
    return out


POS = column_positions()


def node_center(col_key: str, nid: str) -> tuple[int, int]:
    x, y, w, h = POS[col_key][nid]
    return x + w // 2, y + h // 2


def node_right(col_key: str, nid: str) -> tuple[int, int]:
    x, y, w, h = POS[col_key][nid]
    return x + w, y + h // 2


def node_left(col_key: str, nid: str) -> tuple[int, int]:
    x, y, _, h = POS[col_key][nid]
    return x, y + h // 2


# ---------------------------------------------------------------------------
# SVG primitives
# ---------------------------------------------------------------------------


def text(x: float, y: float, body: str, *, size: int = 12, weight: int = 400,
         fill: str = "#0f172a", anchor: str = "start",
         letter_spacing: float = 0) -> str:
    ls = f' letter-spacing="{letter_spacing}"' if letter_spacing else ""
    return (
        f'<text x="{x:.0f}" y="{y:.0f}" font-size="{size}" font-weight="{weight}" '
        f'fill="{fill}" text-anchor="{anchor}"{ls}>{escape(body)}</text>'
    )


def node_box(col_key: str, nid: str, label: str, status: str) -> str:
    x, y, w, h = POS[col_key][nid]
    fill = STATUS_FILL[status]
    border = STATUS_BORDER[status]
    txtcol = STATUS_TEXT[status]
    if status == "explore":
        stroke_dash = ' stroke-dasharray="4 3"'
    else:
        stroke_dash = ""
    if status == "live" and col_key == "input":
        # Dark hero block for the source
        return (
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" '
            f'fill="#0f172a"/>' +
            text(x + w / 2, y + h / 2 + 5, label, size=14, weight=700,
                 fill="white", anchor="middle")
        )
    return (
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="9" '
        f'fill="{fill}" stroke="{border}" stroke-width="1.4"{stroke_dash}/>'
        + text(x + w / 2, y + h / 2 + 5, label, size=12, weight=600,
               fill=txtcol, anchor="middle")
    )


def cubic_edge(x1: float, y1: float, x2: float, y2: float, *,
               stroke: str, width: float, dashed: bool = False,
               opacity: float = 1.0,
               marker: str | None = None) -> str:
    dx = (x2 - x1) * 0.45
    cp1x, cp1y = x1 + dx, y1
    cp2x, cp2y = x2 - dx, y2
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    mk = f' marker-end="url(#{marker})"' if marker else ""
    return (
        f'<path d="M {x1:.1f} {y1:.1f} C {cp1x:.1f} {cp1y:.1f}, '
        f'{cp2x:.1f} {cp2y:.1f}, {x2:.1f} {y2:.1f}" '
        f'fill="none" stroke="{stroke}" stroke-width="{width}" '
        f'stroke-opacity="{opacity}"{dash}{mk}/>'
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

    # Defs: arrowheads (one per shipped lane color, plus a gray one for explore)
    marker_defs: list[str] = []
    for lane in LANES:
        cid = "ar_" + lane["color"].lstrip("#")
        marker_defs.append(
            f'<marker id="{cid}" markerWidth="9" markerHeight="9" '
            f'refX="8" refY="3" orient="auto" markerUnits="strokeWidth">'
            f'<path d="M0,0 L0,6 L8,3 z" fill="{lane["color"]}"/></marker>'
        )
    marker_defs.append(
        '<marker id="ar_grey" markerWidth="8" markerHeight="8" '
        'refX="7" refY="3" orient="auto" markerUnits="strokeWidth">'
        '<path d="M0,0 L0,6 L7,3 z" fill="#94a3b8"/></marker>'
    )
    parts.append('<defs>' + "".join(marker_defs) + '</defs>')

    # Background
    parts.append(f'<rect width="{W}" height="{H}" fill="#f8fafc"/>')

    # Title block
    parts.append(text(40, 56,
                      "The design space \u2014 every dial we could turn",
                      size=28, weight=800, fill="#0f172a"))
    parts.append(text(40, 84,
                      "Bold colored lanes are what's running today. Dashed "
                      "boxes are alternative slices, pre-/post-processing and "
                      "model heads we could plug in without re-designing the "
                      "system.",
                      size=13, fill="#64748b"))

    # Column headers
    for key, header, x, w, color in COLUMNS:
        parts.append(text(x + w / 2, 124, header, size=10, weight=700,
                          fill="#94a3b8", anchor="middle", letter_spacing=2))

    # Faint vertical column separators
    for key, _, x, w, _ in COLUMNS[:-1]:
        sep_x = x + w + 30
        parts.append(
            f'<line x1="{sep_x}" y1="140" x2="{sep_x}" y2="1050" '
            f'stroke="#e2e8f0" stroke-width="1" stroke-dasharray="3 4"/>'
        )

    # Exploratory edges first (so they sit behind everything else)
    for fc, fid, tc, tid in EXPLORE_EDGES:
        x1, y1 = node_right(fc, fid)
        x2, y2 = node_left(tc, tid)
        parts.append(cubic_edge(x1, y1, x2, y2,
                                stroke="#cbd5e1", width=1.0,
                                dashed=True, opacity=0.85))

    # Status legend strip (top-right)
    legend_x = W - 540
    legend_y = 50
    legend_items = [
        ("live", "shipped today"),
        ("demo", "shipped \u00b7 pipe demo only"),
        ("gated", "code shipped \u00b7 awaiting weights"),
        ("explore", "explored alternative"),
    ]
    cursor = legend_x
    for status, lbl in legend_items:
        sw_w = 16
        parts.append(
            f'<rect x="{cursor}" y="{legend_y}" width="{sw_w}" height="14" '
            f'rx="3" fill="{STATUS_FILL[status]}" '
            f'stroke="{STATUS_BORDER[status]}" stroke-width="1.2"'
            + (' stroke-dasharray="3 2"' if status == "explore" else '')
            + '/>'
        )
        parts.append(text(cursor + sw_w + 6, legend_y + 11, lbl,
                          size=11, fill="#475569"))
        cursor += sw_w + 6 + len(lbl) * 6.6 + 18

    # Shipped lane edges (drawn before nodes so the boxes cover any overlap)
    # Each lane gets a slight vertical jitter at hop endpoints so multiple
    # lanes through the same node can be told apart.
    LANE_JITTER = 6  # px per lane spread

    # Determine which lanes pass through each shared (col, id) hop, so we can
    # offset their attachment points.
    hop_lanes: dict[tuple[str, str], list[int]] = {}
    for li, lane in enumerate(LANES):
        for hop in lane["hops"]:
            hop_lanes.setdefault(hop, []).append(li)

    def offset(col_key: str, nid: str, lane_idx: int) -> int:
        lanes = hop_lanes[(col_key, nid)]
        if len(lanes) <= 1:
            return 0
        rank = lanes.index(lane_idx)
        n = len(lanes)
        return int((rank - (n - 1) / 2) * LANE_JITTER)

    for li, lane in enumerate(LANES):
        color = lane["color"]
        marker = "ar_" + color.lstrip("#")
        hops = lane["hops"]
        for j in range(len(hops) - 1):
            fc, fid = hops[j]
            tc, tid = hops[j + 1]
            x1, y1 = node_right(fc, fid)
            x2, y2 = node_left(tc, tid)
            y1 += offset(fc, fid, li)
            y2 += offset(tc, tid, li)
            is_last = j == len(hops) - 2
            parts.append(cubic_edge(
                x1, y1, x2, y2,
                stroke=color, width=2.6,
                dashed=False, opacity=0.95,
                marker=marker if is_last else None,
            ))

    # Nodes (drawn over edges so boxes always read clean)
    for key, _, _, _, _ in COLUMNS:
        for nid, label, status in NODES[key]:
            parts.append(node_box(key, nid, label, status))

    # Lane key on the bottom: list the five shipped lanes with their colors
    parts.append(text(40, 1010, "shipped pipelines", size=10, weight=700,
                      fill="#94a3b8", letter_spacing=2))
    cursor = 40
    chip_y = 1024
    for lane in LANES:
        chip_w = 14
        parts.append(
            f'<rect x="{cursor}" y="{chip_y}" width="{chip_w}" height="14" '
            f'rx="3" fill="{lane["color"]}"/>'
        )
        parts.append(text(cursor + chip_w + 6, chip_y + 11, lane["name"],
                          size=11, weight=600, fill="#0f172a"))
        cursor += chip_w + 6 + len(lane["name"]) * 6.8 + 22

    # Footer note
    parts.append(text(40, 1072,
                      "Read left \u2192 right: every video can be fed to the "
                      "AI as the whole asset, as cut clips or as sampled "
                      "frames; pre- and post-processing slot in on either side "
                      "of the model. Today's stack uses passthrough for video, "
                      "1 fps sampling for frames, and 6 s cuts for clip text.",
                      size=11, fill="#64748b"))

    parts.append("</svg>")
    return "\n".join(parts)


def main() -> None:
    OUT.write_text(build())
    print(f"wrote {OUT.relative_to(OUT.parent.parent.parent)}")


if __name__ == "__main__":
    main()
