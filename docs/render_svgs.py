"""Render every `docs/architecture/*.svg` to a sibling PNG at 2x scale.

Uses headless Chrome (the only converter available on this machine). Each
SVG's intrinsic viewBox dimensions are read directly so the output PNG
matches the diagram's true aspect ratio and never distorts.

Run with::

    python3 docs/render_svgs.py
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ARCH_DIR = Path(__file__).resolve().parent / "architecture"
CHROME = "/usr/bin/google-chrome"
SCALE = 2  # 2x renders crisp on retina + slide projection

VIEWBOX_RE = re.compile(r'viewBox="\s*0\s+0\s+([\d.]+)\s+([\d.]+)\s*"')


def viewbox(svg_path: Path) -> tuple[int, int]:
    text = svg_path.read_text()
    m = VIEWBOX_RE.search(text)
    if not m:
        raise SystemExit(f"no viewBox in {svg_path}")
    return int(float(m.group(1))), int(float(m.group(2)))


def render(svg_path: Path) -> Path:
    out_png = svg_path.with_suffix(".png")
    w, h = viewbox(svg_path)
    win_w, win_h = w * SCALE, h * SCALE

    with tempfile.TemporaryDirectory() as tmp:
        # Wrap the SVG in a tiny HTML page that forces it to fill the
        # window exactly, with no scrollbars / white margins.
        html = (
            "<!doctype html><html><head><style>"
            "html,body{margin:0;padding:0;background:white;}"
            f"svg{{display:block;width:{win_w}px;height:{win_h}px;}}"
            "</style></head><body>"
            + svg_path.read_text()
            + "</body></html>"
        )
        # Re-tag any width/height on the root <svg> so it stretches to
        # the wrapper size. Easiest: inject style attribute via regex.
        host = Path(tmp) / "host.html"
        host.write_text(html)

        cmd = [
            CHROME,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--hide-scrollbars",
            f"--screenshot={out_png}",
            f"--window-size={win_w},{win_h}",
            "--default-background-color=FFFFFFFF",
            "--virtual-time-budget=2000",
            host.as_uri(),
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True)

    print(f"  {svg_path.name:30s} {w}x{h}  ->  {out_png.name} ({win_w}x{win_h})")
    return out_png


def main() -> int:
    svgs = sorted(ARCH_DIR.glob("*.svg"))
    if not svgs:
        print("no SVGs found", file=sys.stderr)
        return 1
    print(f"rendering {len(svgs)} SVGs at {SCALE}x:")
    for s in svgs:
        render(s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
