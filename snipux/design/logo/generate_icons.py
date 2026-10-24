"""Generates every app-icon size, and the large `snipux.png`, from the
Snipux mark: four lime corner brackets and the select-tool cursor.

The large sizes used to be a downscale of an older, detailed master (a
dark rounded window with title-bar dots, a dashed marquee and a cursor),
and only 16/24/32px were drawn from this simplified mark -- SNX-102, because
the detailed scene blurs into nothing below 48px. That left two different
logos depending on size: the Start menu tile and the Linux app grid showed
the old one, the title bar and the tray the new one. Now every size is this
mark, drawn directly at its own resolution rather than scaled.

Run this after changing the design (colours, proportions, the cursor
shape) to regenerate `snipux-<size>.png` and `snipux.png` in this
directory. Nothing downstream has to change: `setup_desktop.install_icons()`
(the Linux hicolor theme) and `setup_desktop.render_ico()` (the Windows
.ico, via `packaging/windows/build_icon.py`) both read whatever
`snipux-<size>.png` files are here.

    QT_QPA_PLATFORM=offscreen python snipux/design/logo/generate_icons.py

PyQt6 only, no Pillow/numpy -- CLAUDE.md's "no imaging dependency" rule
applies to build tooling too.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QByteArray, QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import QApplication

from snipux.design import tokens

_HERE = Path(__file__).resolve().parent
_CURSOR_SVG_PATH = _HERE.parent / "icons" / "select.svg"

SIZES = (16, 24, 32, 48, 64, 128, 256, 512)
# The unsized master: what the website and anything wanting a big copy use.
MASTER_SIZE = 1284

# The app's own accent, not a green of its own: the icon used to be
# #60fa46, a fifth green in a palette that already had four within a few
# percent of each other. An icon that does not match the application it
# opens is the one place the difference is seen side by side, in the task
# bar and the title bar at once.
_MARQUEE_GREEN = QColor(tokens.Color.ACCENT)
_CURSOR_FILL = QColor(23, 22, 37)
_CURSOR_OUTLINE = QColor(247, 248, 252)


def _recoloured_cursor_svg(colour: QColor) -> QSvgRenderer:
    """The app's own "select" tool glyph (design/icons/select.svg), rendered
    in `colour` -- reused rather than redrawn, so the logo's cursor and the
    overlay's selection-tool icon are the exact same shape. Both `fill` and
    `stroke` in that SVG are `currentColor` (the path sets fill explicitly,
    the stroke inherits it from the `<svg>` element), so one text
    substitution recolours the whole glyph, the same trick
    `design.icon()` uses at runtime.
    """
    text = _CURSOR_SVG_PATH.read_text(encoding="utf-8").replace("currentColor", colour.name())
    return QSvgRenderer(QByteArray(text.encode("utf-8")))


def _draw_marquee(painter: QPainter, size: int) -> None:
    """The green marquee, distilled to its four corner brackets.

    A full dashed rounded rect (the master's actual marquee) was tried
    first and rendered as a green ring, not a square -- at this pen width
    the straight runs between corners are shorter than a single dash, so
    every side dissolves into the same rounded corners and the shape reads
    as a circle instead of a selection. Corner brackets are what a dashed
    marquee's own corners already look like once the straight dashes
    between them are too small to survive -- four short strokes, unjoined,
    is the marquee distilled to the minimum that still reads as
    "selection" at this size, not a different motif.
    """
    inset = size * 0.12
    arm = size * 0.26
    near, far = inset, size - inset

    pen = QPen(_MARQUEE_GREEN)
    pen.setWidthF(max(1.6, size * 0.15))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)

    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    for corner_x, corner_y, dx, dy in (
        (near, near, 1, 1),  # top-left
        (far, near, -1, 1),  # top-right
        (near, far, 1, -1),  # bottom-left
        (far, far, -1, -1),  # bottom-right
    ):
        path = QPainterPath(QPointF(corner_x + dx * arm, corner_y))
        path.lineTo(corner_x, corner_y)
        path.lineTo(corner_x, corner_y + dy * arm)
        painter.drawPath(path)


def _draw_cursor(painter: QPainter, size: int) -> None:
    """The cursor, anchored over the marquee's bottom-right corner --
    matching the master's own composition -- and sized to be the dominant
    shape in the icon: the whole point of dropping the container and
    chrome at this size is to spend the pixels they used to take on this
    instead.

    Drawn as two passes of the same glyph at two sizes sharing one centre
    point -- a larger, light silhouette first and a smaller, dark one on
    top -- rather than an actual stroke outline, which is what gives the
    master's cursor its light-outline-on-dark look without needing a
    second, hand-built outline path to keep in sync with the glyph.
    """
    centre = QPointF(size * 0.62, size * 0.62)

    fill_size = size * 0.6
    outline_size = fill_size * 1.3

    outline_rect = QRectF(0, 0, outline_size, outline_size)
    outline_rect.moveCenter(centre)
    _recoloured_cursor_svg(_CURSOR_OUTLINE).render(painter, outline_rect)

    fill_rect = QRectF(0, 0, fill_size, fill_size)
    fill_rect.moveCenter(centre)
    _recoloured_cursor_svg(_CURSOR_FILL).render(painter, fill_rect)


def render_icon(size: int) -> QImage:
    """The snipux mark, rendered directly at `size` -- not
    scaled down from a larger render, so every edge is antialiased against
    the actual output resolution rather than being blurred a second time.
    """
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    _draw_marquee(painter, size)
    _draw_cursor(painter, size)
    painter.end()

    return image


def main() -> None:
    # A QApplication is needed for QPainter/QImage/QSvgRenderer even though
    # nothing is shown -- the same requirement test_design.py's `qapp`
    # fixture documents for `design.icon()`.
    app = QApplication.instance() or QApplication([])

    targets = [(size, _HERE / f"snipux-{size}.png") for size in SIZES]
    targets.append((MASTER_SIZE, _HERE / "snipux.png"))
    for size, path in targets:
        if not render_icon(size).save(str(path), "PNG"):
            raise SystemExit(f"failed to write {path}")
        print(f"wrote {path}")

    del app


if __name__ == "__main__":
    main()
