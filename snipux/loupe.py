"""The eyedropper's loupe: which pixels of the frozen frame it magnifies,
and where the box and its hex chip go around the pointer.

Both are worked out from plain values -- the frame's image, the pointer,
the monitor under it and the rects of whatever chrome is showing -- so
`OverlayWindow` gathers those, asks here, and paints the answer. Nothing
here holds state or knows about a window.

Separate from `overlay.py` because the placement is its own small problem
with its own history (#106: the loupe used to open under the bar), and it
reads more easily away from the event handling that feeds it. Nothing in
this module imports `overlay.py`.

Coordinates: the pointer, the monitor, the chrome rects and the rects
returned are all in the caller's window-local logical pixels. Only
`magnified` touches image pixels, through the scale it is given.
"""

from __future__ import annotations

from PyQt6.QtCore import QPointF, QRect, QRectF, QSizeF, Qt
from PyQt6.QtGui import QColor, QImage


# The eyedropper's loupe. Its source square is in *logical* px, so it covers
# the same real-world area whatever the monitor's scale factor, and becomes
# image pixels only at crop time.
LOUPE_SOURCE_LOGICAL_SIZE = 20.0
# A fixed logical box, independent of the source's scale factor -- which is
# what keeps the crosshair on the same logical pixel whatever the scaling.
LOUPE_BOX_SIZE = 120
# Off the cursor, so the box never sits on the pixel it is magnifying.
LOUPE_OFFSET = QPointF(20.0, 20.0)
LOUPE_CROSSHAIR_COLOR = QColor(255, 0, 0)


def _hex_of(colour: QColor) -> str:
    """`colour` as a lowercase `#rrggbb` string -- the eyedropper's own
    format, spelled out explicitly rather than trusted to `QColor.name()`,
    whose case is a Qt implementation detail this ticket's "lowercase" is
    not.
    """
    return f"#{colour.red():02x}{colour.green():02x}{colour.blue():02x}"


# The gap between the loupe box and its hex chip.
READOUT_GAP = 6


def magnified(image: QImage, cursor: QPointF, scale: tuple[float, float]) -> QImage | None:
    """The loupe's picture: the `LOUPE_SOURCE_LOGICAL_SIZE` square of
    `image` around `cursor`, blown up to `LOUPE_BOX_SIZE`. None when there
    is nothing to crop.

    `cursor` is in logical pixels and `scale` is `image`'s pixels per
    logical pixel on each axis, so the source square becomes image pixels
    only here, at crop time.
    """
    scale_x, scale_y = scale

    image_cursor_x = cursor.x() * scale_x
    image_cursor_y = cursor.y() * scale_y

    half_width = (LOUPE_SOURCE_LOGICAL_SIZE / 2) * scale_x
    half_height = (LOUPE_SOURCE_LOGICAL_SIZE / 2) * scale_y

    width = min(round(half_width * 2), image.width())
    height = min(round(half_height * 2), image.height())
    if width <= 0 or height <= 0:
        return None

    left = round(image_cursor_x - half_width)
    top = round(image_cursor_y - half_height)
    left = max(0, min(left, image.width() - width))
    top = max(0, min(top, image.height() - height))

    cropped = image.copy(QRect(left, top, width, height))
    return cropped.scaled(
        LOUPE_BOX_SIZE,
        LOUPE_BOX_SIZE,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        # Smoothing would hide the exact pixel this tool exists to read.
        Qt.TransformationMode.FastTransformation,
    )


def rects(
    cursor: QPointF, readout_size: QSizeF, monitor: QRectF, chrome: list[QRectF]
) -> tuple[QRectF, QRectF]:
    """Where the loupe box and its hex chip go for a pointer at `cursor`,
    both as logical rects in `cursor`'s own space (#106). `monitor` is the
    monitor under the pointer and `chrome` every piece of chrome that is
    showing, in that space too.

    The loupe used to open below-right of the pointer, always, and near
    the bottom of a selection that is exactly where the bar sits -- a
    child widget, so it painted over the loupe the user was reading.
    Now the corners are tried in order -- below-right, below-left,
    above-right, above-left, each `LOUPE_OFFSET` from the pointer --
    and the first whose box *and* chip clear every piece of showing
    chrome and fit inside the monitor under the pointer wins. The
    monitor, not the window: the window spans the whole desk, and
    clamping to it can hang the loupe across a bezel or into the gap of
    a staggered layout. If no corner is clear, above-left, clamped into
    that monitor.

    The chip goes on the far side of the box from the pointer -- under a
    box below it, over a box above it -- so it never sits on the spot
    being read.
    """
    box_size = LOUPE_BOX_SIZE
    offset = LOUPE_OFFSET

    def box_at(right: bool, below: bool) -> QRectF:
        x = cursor.x() + offset.x() if right else cursor.x() - offset.x() - box_size
        y = cursor.y() + offset.y() if below else cursor.y() - offset.y() - box_size
        return QRectF(x, y, box_size, box_size)

    for right, below in ((True, True), (False, True), (True, False), (False, False)):
        box = box_at(right, below)
        readout = readout_rect(box, readout_size, below, monitor)
        if (
            monitor.contains(box)
            and monitor.contains(readout)
            and not any(box.intersects(c) or readout.intersects(c) for c in chrome)
        ):
            return box, readout

    box = box_at(False, False)
    box.moveLeft(max(monitor.left(), min(box.left(), monitor.right() - box_size)))
    box.moveTop(max(monitor.top(), min(box.top(), monitor.bottom() - box_size)))
    # Under the box when the clamp pushed it below the pointer.
    below = box.top() > cursor.y()
    return box, readout_rect(box, readout_size, below, monitor)


def readout_rect(box: QRectF, size: QSizeF, below: bool, monitor: QRectF) -> QRectF:
    """The hex chip attached to `box` -- under it when the box is below
    the pointer, over it otherwise -- left-aligned with it and kept
    inside `monitor` sideways. All in one logical space."""
    gap = READOUT_GAP
    x = max(monitor.left(), min(box.left(), monitor.right() - size.width()))
    y = box.bottom() + gap if below else box.top() - gap - size.height()
    return QRectF(QPointF(x, y), size)
