"""The geometry of the selection's frame: its eight handles, the corner
brackets and edge bars drawn for them, the hit targets under them, and
where the two chips above the selection go.

Everything here is a function of a selection rectangle -- a `QRectF` in
whatever space the caller keeps it in, which for `OverlayWindow` is its own
window-local logical pixels -- and returns a path, a rect or a `Handle` in
that same space. Nothing here holds state, paints, or knows about a window.

Separate from `overlay.py` because it is the one part of selection that is
pure arithmetic: `OverlayWindow` decides what the selection *is* and what a
press on a handle means, and asks this module only where things are.
Nothing in this module imports `overlay.py`.
"""

from __future__ import annotations

from enum import Enum

from PyQt6.QtCore import QPointF, QRectF, QSizeF, Qt
from PyQt6.QtGui import QPainterPath, QTransform

from snipux import design


class Handle(Enum):
    """The eight drag handles around an `OverlayWindow` selection.

    Corners double as their own visible chrome -- the corner brackets *are*
    the handle, per docs/design/overlay-redesign.md's "Selection frame"
    section -- while edges get a small rounded bar. Cursor shapes and paint
    geometry are both keyed off these same names so the two can never drift
    apart from each other.
    """

    TOP_LEFT = "top_left"
    TOP_RIGHT = "top_right"
    BOTTOM_LEFT = "bottom_left"
    BOTTOM_RIGHT = "bottom_right"
    TOP = "top"
    BOTTOM = "bottom"
    LEFT = "left"
    RIGHT = "right"


_CORNER_HANDLES = (
    Handle.TOP_LEFT,
    Handle.TOP_RIGHT,
    Handle.BOTTOM_LEFT,
    Handle.BOTTOM_RIGHT,
)
_EDGE_HANDLES = (Handle.TOP, Handle.BOTTOM, Handle.LEFT, Handle.RIGHT)

# Which edge(s) a handle drags. A corner handle frees the two perpendicular
# edges that meet at it; an edge handle frees just its own. Every other edge
# is the anchor for that handle's drag and is never written during it -- this
# is what "the opposite edge/corner stays anchored" (the re-framing
# acceptance criteria) reduces to in _resize_selection below.
_LEFT_HANDLES = (Handle.TOP_LEFT, Handle.BOTTOM_LEFT, Handle.LEFT)
_RIGHT_HANDLES = (Handle.TOP_RIGHT, Handle.BOTTOM_RIGHT, Handle.RIGHT)
_TOP_HANDLES = (Handle.TOP_LEFT, Handle.TOP_RIGHT, Handle.TOP)
_BOTTOM_HANDLES = (Handle.BOTTOM_LEFT, Handle.BOTTOM_RIGHT, Handle.BOTTOM)

# nwse-resize / nesw-resize on corners, ns-resize / ew-resize on edges, per
# the README's "Selection frame" section.
_HANDLE_CURSORS = {
    Handle.TOP_LEFT: Qt.CursorShape.SizeFDiagCursor,
    Handle.BOTTOM_RIGHT: Qt.CursorShape.SizeFDiagCursor,
    Handle.TOP_RIGHT: Qt.CursorShape.SizeBDiagCursor,
    Handle.BOTTOM_LEFT: Qt.CursorShape.SizeBDiagCursor,
    Handle.TOP: Qt.CursorShape.SizeVerCursor,
    Handle.BOTTOM: Qt.CursorShape.SizeVerCursor,
    Handle.LEFT: Qt.CursorShape.SizeHorCursor,
    Handle.RIGHT: Qt.CursorShape.SizeHorCursor,
}


def _l_bracket_local_path(length: float, thickness: float, radius: float) -> QPainterPath:
    """A top-left-oriented L bracket: top and left arms meeting in a box
    `(0, 0, length, length)`, with only the outer corner -- (0, 0), the tip
    that points away from the selection -- rounded.

    Canonical shape that `OverlayWindow._bracket_path` mirrors via
    `QTransform` for the other three corners, rather than re-deriving the
    same outline four times: the design is symmetric across both axes (see
    the reference's four corner `<div>`s, which differ only in which two
    CSS edges/border-radius corner they set).
    """
    path = QPainterPath()
    path.moveTo(radius, 0)
    path.lineTo(length, 0)
    path.lineTo(length, thickness)
    path.lineTo(thickness, thickness)
    path.lineTo(thickness, length)
    path.lineTo(0, length)
    path.lineTo(0, radius)
    # Quarter circle from the left-mid point (0, radius) to the top-mid
    # point (radius, 0): QPainterPath.arcTo's angles run counterclockwise
    # from 0 deg at 3 o'clock, so 180 deg is this rect's left-mid point and
    # a -90 deg sweep is the short way round to the top-mid point, tracing
    # exactly the rounded tip -- not the long way round through the bottom.
    path.arcTo(QRectF(0, 0, 2 * radius, 2 * radius), 180, -90)
    path.closeSubpath()
    return path


# Straddle offsets and corner radii the design specifies by pixel value
# in the README's prose (and the HTML reference) rather than as a named
# `tokens.Metric` constant -- unlike arm length/thickness and handle
# dimensions, which *are* tokenized and read from there below.
CORNER_BRACKET_OFFSET = 2
CORNER_BRACKET_RADIUS = 3
EDGE_HANDLE_OFFSET = 5
EDGE_HANDLE_RADIUS = 5
CORNER_HIT_OFFSET = 7
# The least space between the two chips, and what separates them once
# the pill has to sit beside the size chip instead of at the far corner.
CHIP_GAP = 6


def bracket_path(selection: QRectF, handle: Handle) -> QPainterPath:
    """The filled L-bracket for one corner of `selection`, in the
    selection's own coordinates.

    Built by mirroring the canonical top-left bracket from
    `_l_bracket_local_path` with a `QTransform`, rather than re-deriving
    each corner's outline by hand -- the four corners are the same
    shape reflected across one or both axes and anchored to that
    corner's point on the selection rect, offset outward by
    `CORNER_BRACKET_OFFSET` so the bracket straddles the 1px stroke.
    """
    sel = selection
    metric = design.tokens.Metric
    local = _l_bracket_local_path(
        metric.CORNER_LEN, metric.CORNER_W, CORNER_BRACKET_RADIUS
    )

    flip_x = handle in (Handle.TOP_RIGHT, Handle.BOTTOM_RIGHT)
    flip_y = handle in (Handle.BOTTOM_LEFT, Handle.BOTTOM_RIGHT)
    off = CORNER_BRACKET_OFFSET
    anchor_x = sel.right() + off if flip_x else sel.left() - off
    anchor_y = sel.bottom() + off if flip_y else sel.top() - off

    transform = QTransform()
    transform.translate(anchor_x, anchor_y)
    transform.scale(-1 if flip_x else 1, -1 if flip_y else 1)
    return transform.map(local)


def edge_handle_rect(selection: QRectF, handle: Handle) -> QRectF:
    """The rounded bar for one edge handle of `selection`, in the
    selection's own coordinates: `HANDLE_LONG x HANDLE_SHORT` (or
    transposed for a vertical edge), centred on the edge and offset
    outward by `EDGE_HANDLE_OFFSET` so it overhangs the stroke.
    """
    sel = selection
    metric = design.tokens.Metric
    long_, short = metric.HANDLE_LONG, metric.HANDLE_SHORT
    off = EDGE_HANDLE_OFFSET
    center = sel.center()

    if handle is Handle.TOP:
        return QRectF(center.x() - long_ / 2, sel.top() - off, long_, short)
    if handle is Handle.BOTTOM:
        return QRectF(center.x() - long_ / 2, sel.bottom() - (short - off), long_, short)
    if handle is Handle.LEFT:
        return QRectF(sel.left() - off, center.y() - long_ / 2, short, long_)
    if handle is Handle.RIGHT:
        return QRectF(sel.right() - (short - off), center.y() - long_ / 2, short, long_)
    raise ValueError(f"not an edge handle: {handle!r}")


def corner_hit_rect(selection: QRectF, handle: Handle) -> QRectF:
    """The invisible `HANDLE_HIT`-square hit target for one corner of
    `selection`, in the selection's own coordinates -- offset outward by
    `CORNER_HIT_OFFSET` and centred on the corner point, same as the
    bracket it sits under. No chrome of its own: the bracket is the
    visible handle, per the README.
    """
    sel = selection
    size = design.tokens.Metric.HANDLE_HIT
    off = CORNER_HIT_OFFSET

    if handle is Handle.TOP_LEFT:
        return QRectF(sel.left() - off, sel.top() - off, size, size)
    if handle is Handle.TOP_RIGHT:
        return QRectF(sel.right() - size + off, sel.top() - off, size, size)
    if handle is Handle.BOTTOM_LEFT:
        return QRectF(sel.left() - off, sel.bottom() - size + off, size, size)
    if handle is Handle.BOTTOM_RIGHT:
        return QRectF(sel.right() - size + off, sel.bottom() - size + off, size, size)
    raise ValueError(f"not a corner handle: {handle!r}")


def handle_at(selection: QRectF, pos: QPointF) -> Handle | None:
    """Which handle of `selection` (if any) a point in the selection's own
    coordinates falls in.

    Corners checked first: a very small selection could bring a corner
    hit target and an edge handle's rect into overlap, and the corner
    bracket is the more specific, visually on-top target of the two.
    """
    for handle in _CORNER_HANDLES:
        if corner_hit_rect(selection, handle).contains(pos):
            return handle
    for handle in _EDGE_HANDLES:
        if edge_handle_rect(selection, handle).contains(pos):
            return handle
    return None


def chip_rects(
    selection: QRectF, bounds: QRectF, chip_size: QSizeF, pill_size: QSizeF
) -> tuple[QRectF, QRectF]:
    """Where the dimension chip and the Frozen pill go, in the selection's
    own coordinates (logical): above the selection's two top corners, as the
    handoff has them, for as long as that is on the selection's own
    monitor and leaves the two apart.

    They used to go there regardless. A selection touching the top of
    its monitor -- Full screen always does -- put them on the monitor
    above, in the gap beside it, or off the desk altogether; and on a
    selection narrower than the pair they were drawn over each other.

    With no room above, both move inside the selection's top-left
    corner, inset by the gap they keep from its edge when above. The
    pill goes beside the chip there rather than to the far corner,
    which on a selection the size of its monitor is where the close
    button is. It goes beside the chip too whenever the selection is
    too narrow to hold them apart. Either way the pair is then held
    inside `bounds`, the usable area of the selection's monitor.
    """
    sel = selection

    top = sel.top() - design.tokens.Metric.CHIP_OFFSET_Y
    left = sel.left()
    above = top >= bounds.top()
    if not above:
        inset = design.tokens.Metric.CHIP_OFFSET_Y - chip_size.height()
        top = max(sel.top(), bounds.top()) + inset
        left += inset
    left = max(bounds.left(), min(left, bounds.right() - chip_size.width()))
    chip = QRectF(QPointF(left, top), chip_size)
    pill = QRectF(QPointF(sel.right() - pill_size.width(), top), pill_size)
    if above and pill.left() >= chip.right() + CHIP_GAP:
        pill.moveRight(min(pill.right(), bounds.right()))
        return chip, pill

    pill.moveLeft(chip.right() + CHIP_GAP)
    overhang = max(0.0, pill.right() - bounds.right())
    chip.translate(-overhang, 0)
    pill.translate(-overhang, 0)
    return chip, pill
