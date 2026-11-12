#!/usr/bin/env python3
"""Draw the comparison pictures for the logo proposals in docs/design/logo/.

Each proposal is one file, docs/design/logo/<key>/mark.svg: a transparent
360x360 mark drawn with three colour tokens. This script puts that mark where
the project shows its logo (the app icon, the home screen, a browser tab, the
docs header) in each colourway, and writes:

    docs/design/logo/<key>/sheet.webp    one proposal in every place
    docs/design/logo/<key>/icon-<colourway>.svg   the icon on its plate
    docs/design/logo/overview.webp       every proposal side by side
    docs/design/logo/pixels.webp         every proposal at 32 and 16 real pixels

The sheets and the overview are drawn at twice their size, so a 16 px sample
there covers 32 pixels. pixels.webp is the one picture at real size: each icon
is rendered at exactly 32 and 16 pixels, and shown again with every pixel
enlarged to a square.

Needs rsvg-convert and ImageMagick (magick). Run from anywhere:

    python3 docs/design/rig/logo-sheet.py
"""

import base64
import re
import subprocess
import sys
import tempfile
from pathlib import Path

LOGO_DIR = Path(__file__).resolve().parents[1] / "logo"

PLATE = "#0E1621"  # banner and docs background
PANEL = "#17212B"  # viewer sidebar, Telegram Night
DEEP = "#070C12"  # banner side panel
TEXT = "#F2F6FA"
DIM = "#8FA3B5"

# The marks are drawn in the first colourway; the others are text replacements.
TOKENS = ("#2AABEE", "#C5E4F3", "#7FC1EA")
COLOURWAYS = {
    "blue": ("#2AABEE", "#C5E4F3", "#7FC1EA"),  # the docs accent today
    "teal": ("#2DD4BF", "#CCFBF1", "#5EEAD4"),  # the iOS app's own accent
}

NAMES = {
    "a-stack": "A. Stack",
    "b-chat-stack": "B. Chat stack",
    "c-box": "C. Kept messages",
    "d-history": "D. History",
    "e-card-index": "E. Card index",
    "f-pile": "F. Pile",
}

# On its plate the mark is drawn at this share of the icon, as the old icon was.
ICON_SCALE = 0.84

FONT = "font-family='Helvetica Neue, Helvetica, Arial, sans-serif'"


def inner(svg_text: str) -> str:
    """The contents of an SVG file without its outer <svg> element."""
    body = re.sub(r"<\?xml[^>]*\?>", "", svg_text)
    body = re.sub(r"^.*?<svg[^>]*>", "", body, count=1, flags=re.S)
    return re.sub(r"</svg>\s*$", "", body, flags=re.S)


def recolour(body: str, colourway: str, suffix: str) -> str:
    for token, colour in zip(TOKENS, COLOURWAYS[colourway], strict=True):
        body = body.replace(token, colour)
    # Ids must stay unique when one page holds the same mark several times.
    ids = set(re.findall(r'id="([^"]+)"', body))
    for name in ids:
        body = body.replace(f'id="{name}"', f'id="{name}-{suffix}"')
        body = body.replace(f"url(#{name})", f"url(#{name}-{suffix})")
    return body


class Page:
    def __init__(self, width: int, height: int):
        self.width, self.height, self.parts, self.count = width, height, [], 0
        self.parts.append(f"<rect width='{width}' height='{height}' fill='{DEEP}'/>")

    def text(self, x, y, value, size=15, fill=DIM, weight=400, anchor="start"):
        self.parts.append(
            f"<text x='{x}' y='{y}' {FONT} font-size='{size}' font-weight='{weight}' "
            f"fill='{fill}' text-anchor='{anchor}'>{value}</text>"
        )

    def mark(self, body, colourway, x, y, size):
        self.count += 1
        scale = size / 360
        self.parts.append(
            f"<g transform='translate({x} {y}) scale({scale})'>{recolour(body, colourway, str(self.count))}</g>"
        )

    def icon(self, body, colourway, x, y, size, plate=PLATE, radius=0.2237):
        """The mark on its plate, cut to the iOS icon corner radius."""
        self.parts.append(
            f"<rect x='{x}' y='{y}' width='{size}' height='{size}' "
            f"rx='{size * radius:.2f}' fill='{plate}' stroke='#243140' stroke-width='1'/>"
        )
        inset = size * (1 - ICON_SCALE) / 2
        self.mark(body, colourway, x + inset, y + inset, size * ICON_SCALE)

    def rect(self, x, y, w, h, fill, rx=0):
        self.parts.append(f"<rect x='{x}' y='{y}' width='{w}' height='{h}' rx='{rx}' fill='{fill}'/>")

    def pixels(self, body, colourway, x, y, size, enlarge=1):
        """The icon rendered at exactly `size` pixels, each pixel drawn `enlarge` wide."""
        svg = (
            "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 360 360'>"
            f"<rect width='360' height='360' rx='72' fill='{PLATE}'/>"
            f"<g transform='translate({180 * (1 - ICON_SCALE):.1f} {180 * (1 - ICON_SCALE):.1f}) "
            f"scale({ICON_SCALE})'>{recolour(body, colourway, 'px')}</g></svg>"
        )
        with tempfile.TemporaryDirectory() as tmp:
            src, png = Path(tmp) / "icon.svg", Path(tmp) / "icon.png"
            src.write_text(svg)
            subprocess.run(["rsvg-convert", "-w", str(size), "-h", str(size), "-o", str(png), str(src)], check=True)
            if enlarge > 1:
                subprocess.run(
                    ["magick", str(png), "-filter", "point", "-resize", f"{enlarge * 100}%", str(png)], check=True
                )
            data = base64.b64encode(png.read_bytes()).decode()
        side = size * enlarge
        self.parts.append(
            f"<image x='{x}' y='{y}' width='{side}' height='{side}' href='data:image/png;base64,{data}'/>"
        )

    def write(self, target: Path, zoom: int = 2, lossless: bool = False):
        svg = (
            f"<svg xmlns='http://www.w3.org/2000/svg' width='{self.width}' "
            f"height='{self.height}' viewBox='0 0 {self.width} {self.height}'>" + "".join(self.parts) + "</svg>"
        )
        with tempfile.TemporaryDirectory() as tmp:
            src, png = Path(tmp) / "page.svg", Path(tmp) / "page.png"
            src.write_text(svg)
            quality = ["-define", "webp:lossless=true"] if lossless else ["-quality", "90"]
            subprocess.run(["rsvg-convert", "-z", str(zoom), "-o", str(png), str(src)], check=True)
            subprocess.run(["magick", str(png), *quality, str(target)], check=True)


def sheet(key: str, body: str) -> None:
    page = Page(1200, 560)
    page.text(40, 52, NAMES.get(key, key), size=26, fill=TEXT, weight=800)
    for row, colourway in enumerate(COLOURWAYS):
        top = 84 + row * 232
        page.text(40, top + 16, colourway.capitalize(), size=14)
        # App icon, large.
        page.icon(body, colourway, 40, top + 28, 180)
        # Home screen sizes.
        page.text(260, top + 16, "Home screen, 60 and 40 pt", size=14)
        page.icon(body, colourway, 260, top + 28, 120)
        page.icon(body, colourway, 400, top + 28, 80)
        # Browser tabs, dark and light, at 32 and 16 px.
        page.text(520, top + 16, "Browser tab, 32 and 16 px", size=14)
        for i, (tab, ink) in enumerate(((PANEL, TEXT), ("#FFFFFF", "#1B1F24"))):
            y = top + 28 + i * 62
            page.rect(520, y, 250, 50, tab, rx=8)
            page.icon(body, colourway, 532, y + 9, 32, radius=0.2)
            page.icon(body, colourway, 580, y + 17, 16, radius=0.2)
            page.text(610, y + 30, "Telegram Archive", size=14, fill=ink)
        # Docs header: the bare mark beside the site name.
        page.text(810, top + 16, "Docs header and viewer sidebar", size=14)
        page.rect(810, top + 28, 350, 50, PLATE, rx=8)
        page.mark(body, colourway, 822, top + 38, 30)
        page.text(862, top + 60, "Telegram Archive", size=17, fill=TEXT, weight=800)
        page.rect(810, top + 90, 350, 50, PANEL, rx=8)
        page.mark(body, colourway, 822, top + 100, 30)
        page.text(862, top + 122, "TG Archive", size=17, fill=TEXT, weight=800)
    page.write(LOGO_DIR / key / "sheet.webp")


def overview(marks: dict) -> None:
    cell, pad = 190, 40
    width = pad + len(marks) * (cell + 24) + pad - 24
    page = Page(width, 560)
    for row, colourway in enumerate(COLOURWAYS):
        top = 60 + row * 250
        page.text(pad, top - 18, colourway.capitalize(), size=16, fill=TEXT, weight=800)
        for col, (key, body) in enumerate(marks.items()):
            x = pad + col * (cell + 24)
            page.icon(body, colourway, x, top, cell)
            page.text(x + cell / 2, top + cell + 24, NAMES.get(key, key), size=14, anchor="middle")
    page.write(LOGO_DIR / "overview.webp")


def pixels(marks: dict) -> None:
    """Every proposal at 32 and 16 real pixels, and the same pixels enlarged."""
    row, block = 148, 330
    page = Page(40 + 150 + len(COLOURWAYS) * block, 60 + len(marks) * row)
    for col, colourway in enumerate(COLOURWAYS):
        x = 190 + col * block
        page.text(x, 36, f"{colourway.capitalize()}: 32 and 16 px, then enlarged", size=13)
    for line, (key, body) in enumerate(marks.items()):
        y = 56 + line * row
        page.text(40, y + 70, NAMES.get(key, key), size=14, fill=TEXT, weight=800)
        for col, colourway in enumerate(COLOURWAYS):
            x = 190 + col * block
            page.pixels(body, colourway, x, y, 32)
            page.pixels(body, colourway, x, y + 48, 16)
            page.pixels(body, colourway, x + 48, y, 32, enlarge=4)
            page.pixels(body, colourway, x + 192, y, 16, enlarge=8)
    page.write(LOGO_DIR / "pixels.webp", zoom=1, lossless=True)


def icon_files(key: str, body: str) -> None:
    for colourway in COLOURWAYS:
        svg = (
            "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 360 360' "
            "width='1024' height='1024'>"
            f"<rect width='360' height='360' fill='{PLATE}'/>"
            f"<g transform='translate({180 * (1 - ICON_SCALE):.1f} {180 * (1 - ICON_SCALE):.1f}) "
            f"scale({ICON_SCALE})'>{recolour(body, colourway, colourway)}</g></svg>\n"
        )
        (LOGO_DIR / key / f"icon-{colourway}.svg").write_text(svg)


def main() -> int:
    marks = {path.parent.name: inner(path.read_text()) for path in sorted(LOGO_DIR.glob("*/mark.svg"))}
    if not marks:
        print(f"no docs/design/logo/<key>/mark.svg found under {LOGO_DIR}", file=sys.stderr)
        return 1
    for key, body in marks.items():
        stray = set(re.findall(r"#[0-9A-Fa-f]{6}\b", body)) - set(TOKENS)
        if stray:
            print(f"{key}: colours outside the three tokens: {sorted(stray)}", file=sys.stderr)
            return 1
        sheet(key, body)
        icon_files(key, body)
    overview(marks)
    pixels(marks)
    print(f"drew {len(marks)} proposals: {', '.join(marks)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
