"""Annotation data model and the flattening renderer.

Per CLAUDE.md, annotations are kept as data — never painted directly and
destructively onto a live canvas pixmap — and `render()` flattens an ordered
shape list onto a *copy* of the base image. This buys exact undo, non-
destructive editing, and correct blur/pixelate later, and it is what lets the
same shape list back both the editor's live preview and, eventually, the
final saved image.

`Shape` itself is coordinate-space agnostic — just a bag of `QPointF`s a
`QPainter` draws — but each of its two callers commits to one space and never
mixes them within itself: `Canvas` (editor.py) stores **image-pixel**
coordinates, the same space `Canvas.widget_to_image` produces. `OverlayWindow`
(overlay.py) stores **overlay-window** coordinates instead — local to that
widget's own top-left, the same space its `_selection` already lives in — per
docs/design/overlay-redesign.md's "Ink lives in screen coordinates": marks
never move when the selection is re-framed, only the clip rect drawn over
them does. `render_selection()` below is what bridges that second convention
back to image-pixel space, once, at export -- points and painted lengths
alike, see `Shape._scaled`. Per CLAUDE.md's coordinate-space convention,
each caller's choice is stated once here rather than re-derived per shape.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from typing import ClassVar

from PyQt6.QtCore import Qt, QPointF, QRect, QRectF, QSizeF
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QPolygonF,
    QRegion,
    QTransform,
)

from snipux import design
from snipux.capture import Frame


@dataclass
class Shape(ABC):
    """Base of the annotation data model. Coordinates are in whichever space
    the caller committed to — see the module docstring.

    Plain dataclasses, not QWidget/QPainter subclasses, so instances stay
    trivially testable and (de)serializable for the later undo/redo ticket.
    """

    colour: QColor
    stroke_width: float
    # Pixels of the space this mark's points are in, per pixel of the space
    # its lengths are in -- see `_scaled`. Set by `render_selection`, never
    # by a caller. Keyword-only so it cannot shift any subclass's own fields
    # out of position.
    length_scale: float = field(default=1.0, kw_only=True)

    # Fixed slack (in whichever coordinate space a shape's own points are
    # in -- see the module docstring) added on top of a stroke-only
    # shape's painted stroke width, or Text's font-derived box, when
    # hit-testing for the eraser. Without it a thin 1px line, or a
    # precisely-placed anchor point (Text/StepMarker), would demand
    # pixel-perfect aim to click. Same value editor.py's own (separate,
    # image-pixel-space) eraser hit-testing already settled on, kept here
    # too since overlay-window-space marks want the same feel.
    HIT_TOLERANCE: ClassVar[float] = 6.0

    @abstractmethod
    def draw(self, painter: QPainter) -> None:
        """Paint this shape onto `painter`'s active device."""

    def hit_test(self, point: QPointF) -> bool:
        """Whether `point` -- in this shape's own coordinate space, per the
        module docstring -- lands on this mark, for the eraser tool.

        Only ever called while the eraser is the active tool
        (docs/design/overlay-redesign.md's "Drawing": "Marks become
        hit-testable only while the eraser is active"), never during
        ordinary drawing, so paying for this is opt-in, not a cost every
        paintEvent carries.

        Base implementation: never a hit -- the safe default for a shape
        type that hasn't opted in below, mirroring
        `ObscuringShape.draw()`'s "fail loudly if reached unexpectedly"
        spirit but returning False instead of raising: a shape the eraser
        can't reason about should never be silently removed, but a stray
        click landing on one shouldn't crash the eraser either. Every
        concrete shape a committed mark can actually be overrides this.
        """
        return False

    def _scaled(self, length: float) -> float:
        """`length`, one this mark paints with, in the pixels its points are in.

        A mark's points and its lengths -- stroke width, font size, corner
        and badge radii, arrowhead, a label's padding -- start out in one
        space, the one it was drawn in: logical pixels on the overlay, which
        the window's device turns physical, or image pixels in the review
        window. `render_selection` then moves the points into the crop's
        physical pixels, and every length has to follow them there, or a 6px
        stroke drawn on a 1.5x monitor exports 6 pixels wide instead of the 9
        it covered on screen. `length_scale` is that move's ratio, image
        pixels per logical pixel.

        Everywhere else it is 1.0, and this returns `length` exactly -- a
        float times 1.0 is that float -- which is what keeps every mark
        painting byte for byte as it did before lengths scaled. Every length
        a shape paints with goes through here, so the constants stay in the
        logical pixels the design gives them in.
        """
        return length * self.length_scale

    def _pen(self) -> QPen:
        pen = QPen(self.colour)
        pen.setWidthF(self._scaled(self.stroke_width))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        return pen

    def interior_hit_test(self, point: QPointF) -> bool:
        """Whether `point` falls inside this shape's *enclosed area*, for
        the shapes that have one.

        False for everything by default, and deliberately not folded into
        `hit_test`: the stroked outline is the shape, and clicking the empty
        middle of a box is clicking whatever is behind it. The eraser uses
        this only as a second pass, once nothing at all was hit -- which is
        what stops "click the box, box goes" from also meaning "click near
        the mark inside the box, box goes".
        """
        return False

    def _stroke_hit_test(self, path: QPainterPath, point: QPointF) -> bool:
        """Shared by every stroke-only shape's hit_test: widen `path` --
        the same outline draw() strokes -- by this shape's actual painted
        stroke width plus HIT_TOLERANCE, then test `point` against the
        widened region. Reads the width from `self._pen()` rather than
        `self.stroke_width` directly, so a subclass that widens its own
        pen for painting (Highlighter, via its `_pen()` override) gets a
        hit region that matches what it actually draws, without that
        formula being duplicated here -- this is what satisfies "a hit
        test on a stroked shape allows for the stroke width."
        """
        stroker = QPainterPathStroker()
        stroker.setWidth(self._pen().widthF() + self.HIT_TOLERANCE)
        return stroker.createStroke(path).contains(point)


def _rect_from_corners(start: QPointF, end: QPointF) -> QRectF:
    return QRectF(start, end).normalized()


def _polyline_path(points: list[QPointF]) -> QPainterPath | None:
    """A QPainterPath tracing `points` as connected line segments, or None
    if there are fewer than two to connect -- shared by Pen/Highlighter's
    hit_test, which both stroke a plain polyline the same way their draw()
    does.
    """
    if len(points) < 2:
        return None
    path = QPainterPath()
    path.moveTo(points[0])
    for point in points[1:]:
        path.lineTo(point)
    return path


# Line style name -> (on, off) in pixels, () for solid -- see `_line_pen`.
_DASH_PATTERNS = {name: pattern for name, pattern, _label in design.tokens.DASH_CYCLE}


def _check_style(shape: Shape) -> None:
    """Refuse a fill or line style the handoff does not name.

    Checked when the mark is made rather than when it is painted: an unknown
    name found by `draw()` raises from inside a paintEvent, and goes on
    raising on every repaint after it.
    """
    fill = getattr(shape, "fill", "outline")
    if fill not in design.tokens.FILL_OPACITY:
        raise ValueError(f"unknown fill {fill!r}")
    if shape.dash not in _DASH_PATTERNS:
        raise ValueError(f"unknown line style {shape.dash!r}")


def _line_pen(shape: Shape) -> QPen:
    """`shape._pen()` in the shape's own line style, `dash`.

    The handoff's patterns are SVG dash arrays -- lengths in pixels -- and
    `QPen.setDashPattern` counts in multiples of the pen's width instead.
    Handed over as they stand, `9 7` at a 5px stroke is a 45px dash and a
    35px gap, growing with every step thicker. Dividing by the width keeps
    the handoff's lengths at every stroke width.

    The cap goes flat for the same reason. Qt caps both ends of every dash,
    so under `_pen()`'s round cap each gap loses a whole stroke width: `9 7`
    has a 5px gap at a 2px stroke and none at all from 8px up. A flat cap
    adds nothing, and is what the handoff's own SVG draws its rectangles and
    ellipses with. Solid returns `_pen()` untouched, round cap and all, which
    is what leaves every mark made before line styles existed exactly as it
    was.

    Coordinates: the handoff's lengths are logical pixels, like the stroke
    width, and go through `Shape._scaled` as it does. The pen's width has
    already been through it, so in an export from a 1.5x monitor a 9px dash
    and a 2px stroke both span 1.5 times the pixels, and the pattern handed
    to Qt -- one divided by the other -- is the one the screen used.
    """
    pen = shape._pen()
    pattern = _DASH_PATTERNS[shape.dash]
    if not pattern:
        return pen
    # Qt paints a zero-width pen one pixel wide and counts its pattern in
    # those pixels.
    width = pen.widthF() or 1.0
    pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    pen.setDashPattern([shape._scaled(length) / width for length in pattern])
    return pen


def _set_fill_and_outline(painter: QPainter, shape: Shape) -> None:
    """Load `painter` with a closed shape's brush and pen, for the one draw
    call that paints both. QPainter fills before it strokes, which is what
    puts `both`'s tint under its outline rather than over it.

    The tint is the stroke colour at `FILL_OPACITY`, never a colour of its
    own: the handoff's rule that a shape has no second colour to reconcile.
    `filled` has no outline at all, as in the handoff -- the tint is the
    whole mark.
    """
    opacity = design.tokens.FILL_OPACITY[shape.fill]
    if opacity:
        tint = QColor(shape.colour)
        tint.setAlphaF(tint.alphaF() * opacity)
        painter.setBrush(tint)
    else:
        painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(Qt.PenStyle.NoPen if shape.fill == "filled" else _line_pen(shape))


@dataclass
class Pen(Shape):
    """Freehand stroke, fully opaque. An ordered list of image-pixel points."""

    points: list[QPointF] = field(default_factory=list)

    def draw(self, painter: QPainter) -> None:
        if len(self.points) < 2:
            return
        painter.setPen(self._pen())
        painter.drawPolyline(QPolygonF(self.points))

    def hit_test(self, point: QPointF) -> bool:
        path = _polyline_path(self.points)
        return path is not None and self._stroke_hit_test(path, point)


@dataclass
class Highlighter(Shape):
    """Freehand stroke at reduced opacity and widened relative to `Pen`.

    Per docs/design/overlay-redesign.md's "Drawing": stroke x
    `Metric.HIGHLIGHT_MULT` at `Metric.HIGHLIGHT_ALPHA`, painted as a single
    stroked path (one `drawPolyline` call with round caps/joins from
    `_pen()`) rather than per-segment strokes, which is what keeps
    overlapping segments of the same sweep from double-darkening at the
    alpha this class paints at.

    Sets/restores opacity immediately around its own draw() so opacity never
    bleeds into shapes drawn after it in the same render() pass.
    """

    points: list[QPointF] = field(default_factory=list)
    # The lines of text the sweep snapped to (`snipux.textsnap`), in the same
    # space as `points`. When there are any they are the mark, and `points`
    # is only the gesture that found them.
    bands: list[QRectF] = field(default_factory=list)

    def _pen(self) -> QPen:
        pen = super()._pen()
        pen.setWidthF(self._scaled(self.stroke_width * design.tokens.Metric.HIGHLIGHT_MULT))
        return pen

    def _band_path(self) -> QPainterPath:
        radius = self._scaled(design.tokens.Metric.HIGHLIGHT_BAND_RADIUS)
        path = QPainterPath()
        # Winding, not the default odd-even: under odd-even the part where two
        # bands overlap counts as outside both, and is left unpainted.
        path.setFillRule(Qt.FillRule.WindingFill)
        for band in self.bands:
            path.addRoundedRect(band, radius, radius)
        # One outline round every band, so two that touch are filled once
        # and do not double-darken where they meet, as the polyline's single
        # stroke does not.
        return path.simplified()

    def draw(self, painter: QPainter) -> None:
        # save()/restore(), not setOpacity(1.0) on the way out: resetting to
        # 1.0 asserts what the painter's opacity *was*, and a caller that had
        # set its own -- `Watermark.paint` multiplies into one -- lost it for
        # every shape drawn after the first highlighter. Restoring hands back
        # whatever was there, which is what this docstring already claimed.
        if len(self.points) < 2 and not self.bands:
            return
        painter.save()
        try:
            painter.setOpacity(
                painter.opacity() * design.tokens.Metric.HIGHLIGHT_ALPHA
            )
            if self.bands:
                painter.fillPath(self._band_path(), self.colour)
            else:
                painter.setPen(self._pen())
                painter.drawPolyline(QPolygonF(self.points))
        finally:
            painter.restore()

    def hit_test(self, point: QPointF) -> bool:
        if self.bands:
            return self._band_path().contains(point) or any(
                band.adjusted(
                    -self.HIT_TOLERANCE, -self.HIT_TOLERANCE, self.HIT_TOLERANCE, self.HIT_TOLERANCE
                ).contains(point)
                for band in self.bands
            )
        # Reuses this class's own _pen() override (stroke x HIGHLIGHT_MULT)
        # via _stroke_hit_test, so the hit region matches the wider stroke
        # actually painted, not the narrower Pen-sized one.
        path = _polyline_path(self.points)
        return path is not None and self._stroke_hit_test(path, point)


@dataclass
class Line(Shape):
    """A straight stroke between two image-pixel points, in any
    `DASH_CYCLE` line style (`dash`; see `_line_pen`)."""

    start: QPointF = field(default_factory=QPointF)
    end: QPointF = field(default_factory=QPointF)
    dash: str = "solid"

    def __post_init__(self) -> None:
        _check_style(self)

    def draw(self, painter: QPainter) -> None:
        painter.setPen(_line_pen(self))
        painter.drawLine(self.start, self.end)

    def hit_test(self, point: QPointF) -> bool:
        path = QPainterPath()
        path.moveTo(self.start)
        path.lineTo(self.end)
        return self._stroke_hit_test(path, point)


@dataclass
class Arrow(Shape):
    """A straight shaft plus a filled arrowhead at `end`.

    Geometry per docs/design/overlay-redesign.md's "Drawing": the head is a
    filled isosceles triangle sized from the stroke width (floored so it
    stays visible at any width but never dwarfs a thin stroke), and the
    shaft stops short of the tip by a fraction of the head's own length so
    a thick shaft's round cap never shows through the filled head.

    `dash` styles the shaft alone. The head is a fill rather than a stroke,
    so it stays solid whatever the shaft is -- as in the handoff, which
    draws it as a polygon beside the dashed line.
    """

    HEAD_LENGTH_MIN = 10.0
    HEAD_LENGTH_FACTOR = 3.4
    HEAD_HALF_WIDTH_MIN = 7.0
    HEAD_HALF_WIDTH_FACTOR = 2.2
    SHAFT_STOP_FRACTION = 0.55

    start: QPointF = field(default_factory=QPointF)
    end: QPointF = field(default_factory=QPointF)
    dash: str = "solid"

    def __post_init__(self) -> None:
        _check_style(self)

    def draw(self, painter: QPainter) -> None:
        dx = self.end.x() - self.start.x()
        dy = self.end.y() - self.start.y()
        shaft_length = math.hypot(dx, dy)

        if shaft_length == 0:
            # Solid whatever `dash` says: a pattern laid along no length at
            # all draws nothing.
            painter.setPen(self._pen())
            painter.drawLine(self.start, self.end)  # degenerate arrow: a dot
            return

        # Sized in logical pixels, floors included, and only then scaled: a
        # floor is a length too, and scaling the stroke but not the floor
        # would export a thin arrow with a smaller head than the screen drew.
        head_length = self._scaled(
            max(self.HEAD_LENGTH_MIN, self.stroke_width * self.HEAD_LENGTH_FACTOR)
        )
        head_half_width = self._scaled(
            max(self.HEAD_HALF_WIDTH_MIN, self.stroke_width * self.HEAD_HALF_WIDTH_FACTOR)
        )

        # Unit vector along the shaft, tip-ward, and its perpendicular --
        # used to build the head's base corners and shorten the shaft
        # without resorting to trig (atan2/cos/sin), unlike the previous
        # angle-based construction this replaces.
        ux, uy = dx / shaft_length, dy / shaft_length
        px, py = -uy, ux

        base_x = self.end.x() - ux * head_length
        base_y = self.end.y() - uy * head_length
        back_left = QPointF(base_x + px * head_half_width, base_y + py * head_half_width)
        back_right = QPointF(base_x - px * head_half_width, base_y - py * head_half_width)

        # Stops short of `end` by SHAFT_STOP_FRACTION of the head's own
        # length -- the design doc's "so it does not poke through the tip".
        shaft_stop = head_length * self.SHAFT_STOP_FRACTION
        shaft_end = QPointF(self.end.x() - ux * shaft_stop, self.end.y() - uy * shaft_stop)
        painter.setPen(_line_pen(self))
        painter.drawLine(self.start, shaft_end)

        head = QPainterPath()
        head.moveTo(self.end)
        head.lineTo(back_left)
        head.lineTo(back_right)
        head.closeSubpath()
        painter.fillPath(head, self.colour)

    def hit_test(self, point: QPointF) -> bool:
        # Hit-tests the shaft alone, ignoring the filled head's triangle --
        # a deliberate simplification (also made in editor.py's own
        # eraser hit-testing) rather than reconstructing the head's exact
        # geometry a second time here; the head sits at the shaft's end
        # and the tolerance already gives a click near it plenty of slack.
        path = QPainterPath()
        path.moveTo(self.start)
        path.lineTo(self.end)
        return self._stroke_hit_test(path, point)


@dataclass
class Rectangle(Shape):
    """A rounded rectangle spanning two image-pixel corners, per
    docs/design/overlay-redesign.md's "Drawing" (3px corner radius).
    `finalize_mark()` below is what normalises a negative width/height on
    release; draw() itself already tolerates either corner order via
    `_rect_from_corners`, for the live in-progress preview.

    `fill` is a `FILL_CYCLE` name: `outline`, the default and the only look
    there was before fills; `filled`, a tint with no outline; or `both`, a
    fainter tint under the outline. `dash` is the outline's `DASH_CYCLE`
    style. See `_set_fill_and_outline` and `_line_pen`.
    """

    CORNER_RADIUS = 3.0

    start: QPointF = field(default_factory=QPointF)
    end: QPointF = field(default_factory=QPointF)
    fill: str = "outline"
    dash: str = "solid"

    def __post_init__(self) -> None:
        _check_style(self)

    def draw(self, painter: QPainter) -> None:
        _set_fill_and_outline(painter, self)
        radius = self._scaled(self.CORNER_RADIUS)
        painter.drawRoundedRect(_rect_from_corners(self.start, self.end), radius, radius)

    def hit_test(self, point: QPointF) -> bool:
        # The stroked outline, plus the interior only when `filled`.
        # Clicking an outline's empty interior is clicking whatever is
        # behind the rectangle, not the rectangle itself, same reasoning
        # editor.py's own hit-testing gives for the analogous case -- and
        # `both`'s faint tint still shows what is behind it. `filled` hides
        # it, and a click must not reach a mark nobody can see ahead of the
        # box covering it: the eraser's first pass runs top to bottom.
        if self.fill == "filled" and self.interior_hit_test(point):
            return True
        path = QPainterPath()
        path.addRect(_rect_from_corners(self.start, self.end))
        return self._stroke_hit_test(path, point)

    def interior_hit_test(self, point: QPointF) -> bool:
        return _rect_from_corners(self.start, self.end).contains(point)


# Below this bounding-box size (in either axis) a drag is treated as a
# stray click rather than a deliberate mark -- docs/design/overlay-redesign.md's
# "Drawing": "shapes need > 3px in either axis". Not named in tokens.py: it
# governs commit-vs-discard at release time, not anything painted.
DROP_THRESHOLD = 3.0


def finalize_mark(shape: Shape) -> Shape | None:
    """The release-time gate between an in-progress drag and the ink layer.

    Returns the shape to commit, or `None` if it should be discarded
    instead -- per docs/design/overlay-redesign.md's "Marks under the
    minimum size are discarded on release", which is what stops a stray
    click from leaving an invisible dot in the undo stack:

    - `Pen`/`Highlighter` need more than one point -- a plain click never
      reaches a second `mouseMoveEvent` to append one.
    - `Arrow`/`Rectangle`/`Ellipse`/`Line`/`Crop`/`ObscuringShape` (`Blur`/
      `Pixelate`)/`Callout` need their bounding box to exceed
      `DROP_THRESHOLD` in at least one axis -- a horizontal or vertical
      drag is a deliberate mark even though it is exactly zero in the
      other axis, so this is an *or*, not an *and*. SNX-64 added
      Ellipse/Line/Crop to this group when it wired them up as overlay.py
      mark tools alongside Rectangle/Arrow -- the same stray-click-vs-
      deliberate-drag distinction the design doc's "Marks under the
      minimum size are discarded on release" describes applies to any
      two-point drag, not just the two this scoped first.

    `Rectangle` additionally gets its `start`/`end` normalised to
    top-left/bottom-right order here, independent of the size check --
    `draw()` already tolerates either order for the live preview (see its
    docstring), but the *committed* shape needs a stable convention for
    later callers (eraser hit-testing, `_transformed`) the way every other
    two-point shape already has by construction. `Ellipse`/`Line`/`Crop`
    keep `Arrow`'s own behaviour instead -- corner order preserved exactly
    as dragged -- since none of their own `draw()`/`hit_test()` cares which
    corner is which the way Rectangle's historical callers did. `Callout`
    keeps that same unnormalised order too, and for a stronger reason than
    "doesn't care": its `start` *is* the point the drag began at, the
    tail's own tip -- normalising it away the way Rectangle's is would
    silently move what the mark is pointing at.
    `ObscuringShape` needs no such normalising either: `apply()` already
    runs its own corners through `_rect_from_corners` internally, so a raw
    start/end pair -- release ahead of press or not -- is already what it
    expects.

    Every other shape (`Text`, `StepMarker`) has no drag-size or
    corner-order notion this ticket scopes, so it is returned unchanged.
    """
    if isinstance(shape, (Pen, Highlighter)):
        return shape if len(shape.points) > 1 else None

    if isinstance(shape, (Arrow, Rectangle, Ellipse, Line, Crop, ObscuringShape, Callout)):
        rect = _rect_from_corners(shape.start, shape.end)
        if rect.width() <= DROP_THRESHOLD and rect.height() <= DROP_THRESHOLD:
            return None
        if isinstance(shape, Rectangle):
            return replace(shape, start=rect.topLeft(), end=rect.bottomRight())
        return shape

    return shape


@dataclass
class Ellipse(Shape):
    """An ellipse bounded by two image-pixel corners, taking `fill` and
    `dash` exactly as `Rectangle` does."""

    start: QPointF = field(default_factory=QPointF)
    end: QPointF = field(default_factory=QPointF)
    fill: str = "outline"
    dash: str = "solid"

    def __post_init__(self) -> None:
        _check_style(self)

    def draw(self, painter: QPainter) -> None:
        _set_fill_and_outline(painter, self)
        painter.drawEllipse(_rect_from_corners(self.start, self.end))

    def hit_test(self, point: QPointF) -> bool:
        # Stroked outline, plus the interior only when `filled` -- same
        # reasoning as Rectangle.hit_test above.
        if self.fill == "filled" and self.interior_hit_test(point):
            return True
        path = QPainterPath()
        path.addEllipse(_rect_from_corners(self.start, self.end))
        return self._stroke_hit_test(path, point)

    def interior_hit_test(self, point: QPointF) -> bool:
        path = QPainterPath()
        path.addEllipse(_rect_from_corners(self.start, self.end))
        return path.contains(point)


def _rounded_pixel_rect(rect: QRectF) -> QRect:
    """Round a normalized QRectF to a QRect the way Frame.crop() does
    (capture.py): both edges rounded independently and width/height taken
    as their difference, not width rounded on its own. Keeps crop/blur/
    pixelate consistent with each other under fractional coordinates, for
    the same tiling reason Frame.crop()'s docstring gives.
    """
    left = round(rect.left())
    top = round(rect.top())
    right = round(rect.right())
    bottom = round(rect.bottom())
    return QRect(left, top, right - left, bottom - top)


def _clamped_pixel_rect(start: QPointF, end: QPointF, image: QImage) -> QRect | None:
    """The image-pixel rect an obscuring shape should sample/replace, or
    None if there's nothing to do.

    None covers two cases: a degenerate rect (start == end, or any zero
    width/height), which happens on every paintEvent of a fresh in-progress
    drag before the user has moved the mouse; and a rect that rounds past
    the image's own bounds, which QImage.copy() would otherwise pad with
    undefined pixels rather than raise on — a silent-corruption risk, not
    just an edge case, so it's clamped here rather than left to the caller.
    """
    rect = _rect_from_corners(start, end)
    if rect.width() <= 0 or rect.height() <= 0:
        return None
    pixel_rect = _rounded_pixel_rect(rect).intersected(image.rect())
    if pixel_rect.width() <= 0 or pixel_rect.height() <= 0:
        return None
    return pixel_rect


@dataclass
class ObscuringShape(Shape):
    """Blur/Pixelate: shapes that replace already-rendered pixels rather
    than paint onto a painter. Unlike every other Shape, correctness here
    depends on reading pixels that reflect every shape drawn before this
    one in the list — render() special-cases this base class, closing the
    QPainter it was holding open before apply() runs and reopening a fresh
    one afterwards, per CLAUDE.md's rule against reading a QPixmap/QImage
    while a QPainter is still active on it.

    `strength` is the tray's Strength slider (docs/design/overlay-redesign.md's
    "Blur tray"), range `Metric.BLUR_MIN`-`Metric.BLUR_MAX`, defaulting to
    `Metric.BLUR_DEFAULT` here so a shape built without the tray (tests,
    programmatic callers) still gets the same effect the UI defaults to.

    `strength` is not one of the lengths `_scaled` stretches, though it
    reads like one -- a pixelate block is about `strength` pixels across.
    It is already in the frozen frame's own physical pixels wherever it
    shows: the overlay bakes a committed blur into the frame image itself
    (`OverlayWindow._base_layer_image`), and `render_selection` applies it
    to the crop of that same image. Screen and export already agree;
    scaling it on export would make the export blockier than the screen by
    the crop's ratio.
    """

    start: QPointF = field(default_factory=QPointF)
    end: QPointF = field(default_factory=QPointF)
    strength: int = design.tokens.Metric.BLUR_DEFAULT

    # The one difference between Blur and Pixelate: which interpolation the
    # final upscale uses. Smooth blends block edges into a blur; nearest
    # neighbour keeps them hard, producing visible blocks. Overridden by
    # each subclass rather than duplicating the rest of apply()'s pipeline.
    # ClassVar so dataclass doesn't turn it into a per-instance __init__
    # field — it's a per-type constant, not shape state.
    _upscale_mode: ClassVar[Qt.TransformationMode] = Qt.TransformationMode.SmoothTransformation

    def apply(self, image: QImage) -> QImage:
        """Return a new QImage: `image` with this shape's rect replaced by
        an obscured version. Does not mutate `image` in place, matching
        the copy-on-write discipline render() itself follows.

        Obscures by downscaling the sampled patch to `1/strength` then
        scaling it back up — per docs/design/overlay-redesign.md's "blur"
        entry. `drawImage()` below replaces the rect's pixels outright
        rather than drawing a translucent effect over the original
        content, which is what makes this destructive: the source pixels
        are gone from the returned image, not merely covered, so they
        cannot be recovered from the export. The averaging a smooth
        downscale performs is what produces the blur/pixelate effect, so
        no manual convolution and no numpy/Pillow/OpenCV dependency, per
        CLAUDE.md.
        """
        pixel_rect = _clamped_pixel_rect(self.start, self.end, image)
        if pixel_rect is None:
            return image  # degenerate/out-of-bounds rect: no-op

        patch = image.copy(pixel_rect)
        small = patch.scaled(
            max(1, pixel_rect.width() // self.strength),
            max(1, pixel_rect.height() // self.strength),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        obscured = small.scaled(
            pixel_rect.width(),
            pixel_rect.height(),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            self._upscale_mode,
        )

        result = QImage(image)
        painter = QPainter(result)
        painter.drawImage(pixel_rect, obscured)
        painter.end()  # closed before this apply() call returns the image
        return result

    def draw(self, painter: QPainter) -> None:
        # Concrete (not abstract) only to satisfy Shape's own abstract
        # draw() — render() never calls this for an ObscuringShape, it
        # calls apply() instead. Raises rather than silently no-op-ing so a
        # caller that bypasses render()'s isinstance check fails loudly.
        raise NotImplementedError("ObscuringShape uses apply(), not draw()")

    def hit_test(self, point: QPointF) -> bool:
        # The whole filled patch counts as the mark, unlike a stroke-only
        # shape's outline -- so no HIT_TOLERANCE slack is added here, same
        # as the analogous case in editor.py's own hit-testing.
        return _rect_from_corners(self.start, self.end).contains(point)


@dataclass
class Blur(ObscuringShape):
    """Obscures its rect by downscaling then upscaling with smooth
    (bilinear) interpolation on both steps — the default `_upscale_mode`
    ObscuringShape.apply() already uses, so nothing to override here beyond
    the type itself.
    """


@dataclass
class Pixelate(ObscuringShape):
    """Obscures its rect the same way `Blur` does, except the final
    upscale uses nearest-neighbour (`FastTransformation`) instead of
    smooth interpolation, producing hard block edges — genuinely
    distinguishable from `Blur`'s soft result over the same region.
    """

    _upscale_mode: ClassVar[Qt.TransformationMode] = Qt.TransformationMode.FastTransformation


@dataclass
class Redact(ObscuringShape):
    """Replaces its rect with opaque black -- the one obscuring shape whose
    result carries nothing of what was underneath.

    Blur and Pixelate average the original pixels, so what they export is
    still a function of the text they covered, and short strings (PINs,
    card numbers, keys) can be recovered by rendering candidates the same
    way and comparing. A solid fill has no such function to invert, which
    is why automatic hiding of sensitive text uses this and not the other
    two.

    `strength` is inherited and ignored: there is no degree of black.
    """

    _FILL = QColor("#000000")

    def apply(self, image: QImage) -> QImage:
        pixel_rect = _clamped_pixel_rect(self.start, self.end, image)
        if pixel_rect is None:
            return image

        result = QImage(image)
        painter = QPainter(result)
        painter.fillRect(pixel_rect, self._FILL)
        painter.end()  # closed before this apply() call returns the image
        return result


@dataclass
class Blackout(Redact):
    """The stills bar's Blackout: a `Redact` in the bar's own near-black.

    A subclass rather than a colour on `Redact` itself, so Hide sensitive's
    boxes keep exactly the fill they have always exported, while
    `_hide_sensitive_text` still counts a region the user blacked out by
    hand as already covered.
    """

    _FILL = QColor(design.tokens.BLACKOUT_FILL)


@dataclass
class Spotlight(ObscuringShape):
    """The inverse of the rest of this family: dims everything *outside*
    its rect instead of replacing what's inside it, so the eye goes where
    it's pointed (docs/design/bars/README.md:78's "spotlight", named
    alongside the callout).

    Joins `ObscuringShape` rather than the handoff's literal "shape
    sibling" wording, a call this ticket asks to be made and written down:
    a plain `Shape` (the `SHAPES` family -- Rectangle/Ellipse/Line/Arrow)
    paints itself once, straight onto whatever painter `_paint_marks` has
    open, clipped to the current selection. A spotlight has to darken
    every pixel outside its rect, not just whatever the selection happens
    to be framing at the moment, and it has to survive a re-frame the way
    a committed blur or redaction already does -- which is exactly the
    "baked into the frame, not painted live" contract `ObscuringShape`
    already carries (`OverlayWindow._base_layer_image`, `render()` below).
    Fitting the existing family costs nothing extra: dragging a rect,
    undo, the eraser and a strength slider all come for free from the
    base class, and `strength` here reads as a dim level instead of a
    block size -- see `_alpha`.

    Several spotlights on one snip do not stack their own `apply()`
    independently: the second one dimming "everything outside *my* rect"
    would redim the first one's hole the moment it ran, leaving only the
    rects' *intersection* lit rather than their union. `apply_all` is what
    `render()` and `_base_layer_image` call instead, once, with every
    spotlight in the mark list -- the same reasoning `_paint_scrim`
    (overlay.py) gives for its own single dim-and-punch fill, though the
    fill itself is a `QRegion` boolean subtraction rather than
    `_paint_scrim`'s even-odd `QPainterPath` -- see `apply_all`'s own
    docstring for why that one hole's trick does not extend to several.
    """

    def _alpha(self) -> float:
        """This mark's own dim level, `strength` read against the
        redaction family's shared 2-20 slider (`tokens.STRENGTH_RANGE`)
        rather than as a block-size divisor.

        Scaled so the slider's own default (`Metric.BLUR_DEFAULT`, the
        `strength` every `ObscuringShape` starts at) reproduces
        `Color.DIM_ALPHA` exactly -- the same dim the selection scrim
        already paints outside the selection, and this app's own existing
        answer to "reads rather than black" rather than a new number
        invented for this mark alone. Clamped to 1.0 so the top of the
        slider is a strong dim, not literally opaque.
        """
        ratio = self.strength / design.tokens.Metric.BLUR_DEFAULT
        return min(1.0, design.tokens.Color.DIM_ALPHA * ratio)

    def apply(self, image: QImage) -> QImage:
        return Spotlight.apply_all(image, [self])

    @staticmethod
    def apply_all(image: QImage, spotlights: list["Spotlight"]) -> QImage:
        """Dim `image` outside the union of every rect in `spotlights`, as
        one fill rather than one per spotlight -- see the class docstring
        for why stacking would leave only the rects' intersection lit.

        Built as a `QRegion` union-then-subtract rather than `_paint_scrim`'s
        even-odd `QPainterPath`: even-odd toggles per subpath crossed, so a
        point inside *two overlapping* holes crosses three subpaths (the
        outer rect and both holes) and comes out filled again -- exactly
        the "second spotlight redims the first's hole" bug this method
        exists to avoid, just moved to wherever two spotlights overlap
        instead of gone. `QRegion.united`/`subtracted` are real set
        operations on the holes first, so an overlap only ever merges,
        never double-counts.

        The darkest of the spotlights' own dim levels wins, since a single
        fill can only carry one alpha: a fainter sibling should not wash
        out a stronger one sharing the same dim.

        Degenerate or out-of-bounds rects drop out silently, same as
        every other `ObscuringShape` (`_clamped_pixel_rect`), and a
        `spotlights` list with nothing left after that is a no-op.
        """
        holes = QRegion()
        alpha = 0.0
        for spot in spotlights:
            pixel_rect = _clamped_pixel_rect(spot.start, spot.end, image)
            if pixel_rect is None:
                continue
            holes = holes.united(QRegion(pixel_rect))
            alpha = max(alpha, spot._alpha())
        if holes.isEmpty():
            return image
        dim_region = QRegion(image.rect()).subtracted(holes)
        colour = QColor(design.tokens.Color.DIM)
        colour.setAlphaF(alpha)

        result = QImage(image)
        painter = QPainter(result)
        painter.setClipRegion(dim_region)
        painter.fillRect(image.rect(), colour)
        painter.end()  # closed before this call returns the image
        return result


@dataclass
class Crop(Shape):
    """A dashed, unfilled rectangle: the live marquee blur and pixelate show
    while they are dragged.

    Historically this was only ever a transient in-progress shape: the old
    editor.py's Canvas built one live during a crop drag, purely so the
    drag got the same live-preview path as Rectangle/Ellipse/etc, and the
    actual crop was performed by `apply_crop()` once the drag ended, which
    flattens and replaces the base image rather than adding to the shape
    list — see its docstring. `OverlayWindow._paint_in_progress_shape`
    still borrows this same dashed style for blur/pixelate's own live
    preview, for the same "cheap marquee, not the real effect" reason.

    SNX-64 also made it a Crop tool on the bar, committed, erased and
    exported the way Rectangle is, and `hit_test` below is what made those
    marks erasable. #78 took the tool away again: it drew a box called Crop
    that cropped nothing, Rectangle draws the same box with a dashed line,
    and the selection's handles are what crop a snip.
    """

    start: QPointF = field(default_factory=QPointF)
    end: QPointF = field(default_factory=QPointF)

    def draw(self, painter: QPainter) -> None:
        pen = self._pen()
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(_rect_from_corners(self.start, self.end))

    def hit_test(self, point: QPointF) -> bool:
        # The dashed stroked outline only -- same "interior isn't the
        # shape" reasoning as Rectangle.hit_test above.
        path = QPainterPath()
        path.addRect(_rect_from_corners(self.start, self.end))
        return self._stroke_hit_test(path, point)


@dataclass
class Text(Shape):
    """An editable label — a click, not a drag — drawn as a background chip
    with the string in the shape's own ink colour on top, per
    docs/design/overlay-redesign.md's "Drawing".

    `point` is the chip's top-left corner (image-pixel space) — the same
    corner the live `QLineEdit` in `Canvas.mousePressEvent` (editor.py) is
    moved to on click — not a text baseline the way this class drew it
    before the redesign.

    Font size is derived from stroke_width (`max(TEXT_FONT_SIZE_MIN,
    stroke_width * TEXT_FONT_SIZE_FACTOR)`) rather than stored separately —
    there is no independent "text size" concept for this ticket, only the
    stroke-width control the toolbar already exposes for every other tool.
    Chrome (background, corner radius, padding, ring) comes from tokens.py
    and is fixed regardless of stroke_width — only the type inside it
    scales. Type and chrome alike are logical-pixel lengths, though, and
    every one of them goes through `_scaled`, so a label exports at the size
    the screen showed it.
    """

    # Design gives this formula directly ("Font size max(12, stroke x 3)"),
    # not a tokens.py entry — same precedent as Arrow's HEAD_LENGTH_MIN/
    # FACTOR above, which also come from prose in the design doc rather
    # than the token dump.
    TEXT_FONT_SIZE_MIN = 12
    TEXT_FONT_SIZE_FACTOR = 3.0

    text: str = ""
    point: QPointF = field(default_factory=QPointF)

    def _font(self) -> QFont:
        # The overlay's own logical size, floor and all, scaled only after.
        # `setPixelSize` takes whole pixels, so at 1.5x a 15px label exports
        # at 22 where the screen drew 22.5.
        logical_size = max(
            self.TEXT_FONT_SIZE_MIN,
            round(self.stroke_width * self.TEXT_FONT_SIZE_FACTOR),
        )
        font = QFont()
        font.setPixelSize(round(self._scaled(logical_size)))
        return font

    def chip_rect(self) -> QRectF:
        """The label chip's actual bounds -- anchor at its top-left, sized
        to the text it holds.

        Shared by `draw` and `hit_test` so the two cannot disagree. They
        did: `hit_test` used to centre a fixed box *on* the anchor while
        `draw` extends right and down from it, so the hit region covered
        one corner of the chip and clicking the label you could see missed
        it. That is what made the eraser look like it ignored labels.
        """
        metrics = QFontMetricsF(self._font())
        pad_h = self._scaled(design.tokens.Metric.TEXT_LABEL_PAD_H)
        pad_v = self._scaled(design.tokens.Metric.TEXT_LABEL_PAD_V)
        return QRectF(
            self.point.x(),
            self.point.y(),
            metrics.horizontalAdvance(self.text) + pad_h * 2,
            metrics.height() + pad_v * 2,
        )

    def draw(self, painter: QPainter) -> None:
        if not self.text:
            return  # an empty string is a no-op, not an error: see PLAN.md

        font = self._font()
        metrics = QFontMetricsF(font)
        pad_h = self._scaled(design.tokens.Metric.TEXT_LABEL_PAD_H)
        pad_v = self._scaled(design.tokens.Metric.TEXT_LABEL_PAD_V)
        chip = self.chip_rect()
        radius = self._scaled(design.tokens.Metric.TEXT_LABEL_RADIUS)

        # Background then ring, each its own drawRoundedRect call at the
        # same geometry -- a stroked pen straddles the fill's edge rather
        # than sitting flush outside it, which is what gives the ring its
        # visible width instead of being swallowed by the fill.
        background = QColor(design.tokens.Color.TEXT_LABEL_BG)
        background.setAlphaF(design.tokens.Color.TEXT_LABEL_BG_ALPHA)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(background)
        painter.drawRoundedRect(chip, radius, radius)

        ring_colour = QColor(design.tokens.Color.TEXT_LABEL_RING)
        ring_colour.setAlphaF(design.tokens.Color.TEXT_LABEL_RING_ALPHA)
        ring_pen = QPen(ring_colour)
        ring_pen.setWidthF(self._scaled(design.tokens.Metric.TEXT_LABEL_RING_W))
        painter.setPen(ring_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(chip, radius, radius)

        painter.setPen(QPen(self.colour))
        painter.setFont(font)
        painter.drawText(
            QPointF(chip.left() + pad_h, chip.top() + pad_v + metrics.ascent()),
            self.text,
        )

    def hit_test(self, point: QPointF) -> bool:
        # The chip draw() actually paints, widened by the usual tolerance.
        # Anything else and the label you can see is not the label you can
        # click -- see `chip_rect`.
        return self.chip_rect().adjusted(
            -self.HIT_TOLERANCE, -self.HIT_TOLERANCE,
            self.HIT_TOLERANCE, self.HIT_TOLERANCE,
        ).contains(point)


def _wrap_text(text: str, metrics: QFontMetricsF, max_width: float) -> list[str]:
    """`text` broken into lines no wider than `max_width` per `metrics` --
    greedy word wrap: each line takes words until the next one would
    overflow it, then starts a new line. A single word wider than
    `max_width` on its own still gets a line of its own rather than being
    split mid-word, the same call a word processor makes.

    Shared by `Callout.draw()` and its own `wrapped_lines()`, so the lines
    a test works out from a `QFontMetrics` are the same ones actually
    painted -- see CLAUDE.md's rule against a test assuming this
    machine's fonts.
    """
    words = text.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if current and metrics.horizontalAdvance(candidate) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    lines.append(current)
    return lines


@dataclass
class Callout(Shape):
    """A rounded-rect body plus a triangular tail, with text that wraps
    inside it -- an arrow and a label that move, undo and erase as the one
    mark docs/design/bars/README.md:78 names it a shape sibling for
    ("Rounded-rect, polygon, callout, spotlight -> shape siblings"), rather
    than two marks lined up by hand.

    Geometry, in one gesture -- the handoff leaves the drag itself
    unspecified, so this is this ticket's own choice, recorded here rather
    than left to be re-derived: `start` is where the drag began, and stays
    the tail's own tip -- the point the mark is pointing at. `end` is the
    corner dragged to. The body is the drag's own bounding rect
    (`_rect_from_corners(start, end)`, exactly `Rectangle`'s own), pulled
    in by `TAIL_LENGTH` at whichever corner `start` sits on, so that corner
    is left as a notch the tail fills rather than `start` landing exactly
    on the body's own outline -- which would draw a tail of zero length.
    `_geometry()` works out the body and the tail's triangle together,
    once, the way `Text.chip_rect()` does, for the same "two callers must
    not disagree" reason.

    Text reuses `Text`'s own click-then-type editor rather than a second
    one: `TextLabelEditor.begin`'s `on_commit` (driven from
    `OverlayWindow._start_callout_text_entry`, and `ImageCanvas`'s own
    twin in review.py) fires once the field commits, with whatever was
    typed -- empty included -- baked into the one `Callout` added to the
    mark store then. That is what keeps body, tail and text a single undo
    step: nothing is ever added twice, and nothing already committed is
    mutated afterwards. Escape/abandon commits the callout as dragged,
    with the field cleared first -- the body and tail the drag placed
    survive; only a half-typed word is what gets discarded, the same split
    `Text`'s own abandon makes (`TextLabelEditor.abandon`). Re-editing an
    already-committed callout's text is out of scope here for the same
    reason it is for `Text`: neither mark supports it.

    Unlike `Text`'s chip, the body does not grow to fit what is typed --
    it is the size the drag gave it, per the ticket's "text wraps inside
    the body rather than overflowing it." `draw()` wraps greedily
    (`_wrap_text`) at the body's own padded width and clips to its padded
    height, so text past either edge is wrapped, never spilled outside the
    box; text past the bottom is clipped rather than growing the box to
    fit, the same trade any fixed-size box makes.
    """

    TAIL_LENGTH = 16.0
    CORNER_RADIUS = Rectangle.CORNER_RADIUS
    # The same formula `Text._font` uses, kept as this class's own constant
    # rather than shared -- `Arrow`'s HEAD_LENGTH_MIN/FACTOR are the same
    # precedent -- since a callout has no other reason to depend on Text.
    TEXT_FONT_SIZE_MIN = 12
    TEXT_FONT_SIZE_FACTOR = 3.0

    start: QPointF = field(default_factory=QPointF)
    end: QPointF = field(default_factory=QPointF)
    text: str = ""
    fill: str = "outline"
    dash: str = "solid"

    def __post_init__(self) -> None:
        _check_style(self)

    def _geometry(self) -> tuple[QRectF, QPolygonF | None]:
        """`(body, tail)` -- see the class docstring. `tail` is None when
        the drag left no room for a notch on some axis (a sliver-thin
        callout); the body still draws, just without one.
        """
        full = _rect_from_corners(self.start, self.end)
        notch = self._scaled(self.TAIL_LENGTH)
        notch_x = min(notch, full.width() / 2)
        notch_y = min(notch, full.height() / 2)
        near_left = self.start.x() <= self.end.x()
        near_top = self.start.y() <= self.end.y()
        left, right = full.left(), full.right()
        top, bottom = full.top(), full.bottom()
        if near_left:
            left += notch_x
        else:
            right -= notch_x
        if near_top:
            top += notch_y
        else:
            bottom -= notch_y
        body = QRectF(QPointF(left, top), QPointF(right, bottom))
        if notch_x <= 0 or notch_y <= 0:
            return body, None
        corner_x = body.left() if near_left else body.right()
        corner_y = body.top() if near_top else body.bottom()
        base_a = QPointF(corner_x, corner_y + (notch_y if near_top else -notch_y))
        base_b = QPointF(corner_x + (notch_x if near_left else -notch_x), corner_y)
        return body, QPolygonF([self.start, base_a, base_b])

    def body_rect(self) -> QRectF:
        """The body alone -- for callers (the text editor's own placement,
        tests) that only need where the box sits, not the tail too.
        """
        return self._geometry()[0]

    def _combined_path(self) -> QPainterPath:
        """The body's rounded rect and the tail's triangle, unioned into
        one outline -- so `draw()`'s stroke wraps both without a seam
        where the tail meets the body, and `hit_test`/`interior_hit_test`
        read the mark as the one shape it is.
        """
        body, tail = self._geometry()
        radius = self._scaled(self.CORNER_RADIUS)
        path = QPainterPath()
        path.addRoundedRect(body, radius, radius)
        if tail is not None:
            tail_path = QPainterPath()
            tail_path.addPolygon(tail)
            tail_path.closeSubpath()
            path = path.united(tail_path)
        return path

    def _font(self) -> QFont:
        logical_size = max(
            self.TEXT_FONT_SIZE_MIN, round(self.stroke_width * self.TEXT_FONT_SIZE_FACTOR)
        )
        font = QFont()
        font.setPixelSize(round(self._scaled(logical_size)))
        return font

    def _text_rect(self, body: QRectF) -> QRectF:
        pad_h = self._scaled(design.tokens.Metric.TEXT_LABEL_PAD_H)
        pad_v = self._scaled(design.tokens.Metric.TEXT_LABEL_PAD_V)
        return body.adjusted(pad_h, pad_v, -pad_h, -pad_v)

    def wrapped_lines(self) -> list[str]:
        """`self.text` broken into the lines `draw()` paints, at the
        body's own current padded width -- exposed so a test can compare
        against a `QFontMetrics`-derived expectation without building a
        `QPainter`.
        """
        if not self.text:
            return []
        rect = self._text_rect(self.body_rect())
        metrics = QFontMetricsF(self._font())
        return _wrap_text(self.text, metrics, max(0.0, rect.width()))

    def draw(self, painter: QPainter) -> None:
        _set_fill_and_outline(painter, self)
        painter.drawPath(self._combined_path())
        if not self.text:
            return  # an empty string is a no-op, same as Text's own draw()
        rect = self._text_rect(self.body_rect())
        if rect.width() <= 0 or rect.height() <= 0:
            return
        font = self._font()
        metrics = QFontMetricsF(font)
        lines = _wrap_text(self.text, metrics, rect.width())
        painter.save()
        # Clips vertically as well as horizontally: wrapping alone keeps a
        # long line from overflowing the sides, but more lines than the
        # body is tall still needs somewhere to stop -- see the class
        # docstring's "clipped rather than growing the box".
        painter.setClipRect(rect)
        painter.setFont(font)
        painter.setPen(QPen(self.colour))
        line_height = metrics.height()
        y = rect.top() + metrics.ascent()
        for line in lines:
            if y - metrics.ascent() >= rect.bottom():
                break
            painter.drawText(QPointF(rect.left(), y), line)
            y += line_height
        painter.restore()

    def hit_test(self, point: QPointF) -> bool:
        # Same "outline, plus the interior only when filled" rule as
        # Rectangle.hit_test -- see its own docstring.
        if self.fill == "filled" and self.interior_hit_test(point):
            return True
        return self._stroke_hit_test(self._combined_path(), point)

    def interior_hit_test(self, point: QPointF) -> bool:
        return self._combined_path().contains(point)


@dataclass
class StepMarker(Shape):
    """A filled numbered badge — a click, not a drag.

    `number` is assigned once, by `next_step_number()` below, at the point
    the badge is created — never recomputed afterwards. `render()` just
    paints whatever `number` this instance already carries. That is what
    makes "delete step 2, and 1/3 keep their own numbers"
    (docs/design/overlay-redesign.md's "Drawing": "does not renumber after
    a delete, matches the prototype") fall out for free: there is no
    list-order recomputation left anywhere to disagree with a badge's
    original number.

    Diameter, ring and numeral style all come from tokens.py — `STEP_D`,
    `STEP_RING`/`STEP_RING_W` and `Font.STEP_BADGE`/`Color.ACCENT_FG` — and
    are fixed regardless of `stroke_width`, unlike every drawing tool. The
    design gives the badge a constant size on purpose: a step counter that
    grew every time the user picked a thicker pen would be a strange
    reading experience. Constant in logical pixels, that is: like every
    other length a mark paints with, each of these goes through `_scaled`,
    so an export from a scaled monitor shows the badge the size it was.
    """

    # Qt has no per-shape blur outside a QGraphicsScene (see tokens.Shadow's
    # own docstring, which is about widget-level chrome, not ink flattened
    # onto a QImage) -- so the "soft drop shadow" the design calls for is
    # faked with a handful of concentric, decreasingly-opaque circles rather
    # than pulling in a blur dependency for one shape.
    _SHADOW_LAYERS = 4
    _SHADOW_OFFSET_Y = 3.0
    _SHADOW_SPREAD = 5.0
    _SHADOW_MAX_ALPHA = 0.30

    BADGE_TEXT_COLOUR = QColor(design.tokens.Color.ACCENT_FG)
    # Exposed as a class attribute (not just inlined in _rect()) so callers
    # outside this module -- editor.py's eraser hit-testing chief among
    # them -- can hit-test the exact same circle draw() paints without
    # re-deriving it from tokens.py themselves.
    RADIUS = design.tokens.Metric.STEP_D / 2

    point: QPointF = field(default_factory=QPointF)
    number: int = 0

    def _font(self) -> QFont:
        size, weight = design.tokens.Font.STEP_BADGE
        font = QFont()
        font.setPixelSize(round(self._scaled(size)))
        font.setWeight(QFont.Weight(weight))
        return font

    def _rect(self) -> QRectF:
        radius = self._scaled(self.RADIUS)
        return QRectF(
            self.point.x() - radius, self.point.y() - radius, radius * 2, radius * 2
        )

    def _draw_shadow(self, painter: QPainter, rect: QRectF) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        for layer in range(self._SHADOW_LAYERS, 0, -1):
            # Outermost layer first (biggest, faintest) so each inner layer
            # paints over it rather than the reverse -- otherwise a fainter
            # outer ellipse would be visible on top of a stronger inner one.
            grown = self._scaled(self._SHADOW_SPREAD * layer / self._SHADOW_LAYERS)
            alpha = self._SHADOW_MAX_ALPHA * (1 - (layer - 1) / self._SHADOW_LAYERS)
            shadow_colour = QColor(0, 0, 0)
            shadow_colour.setAlphaF(alpha)
            painter.setBrush(shadow_colour)
            painter.drawEllipse(
                rect.adjusted(-grown, -grown, grown, grown).translated(
                    0, self._scaled(self._SHADOW_OFFSET_Y)
                )
            )

    def draw(self, painter: QPainter) -> None:
        rect = self._rect()

        self._draw_shadow(painter, rect)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.colour)
        painter.drawEllipse(rect)

        ring_pen = QPen(QColor(design.tokens.Color.STEP_RING))
        ring_pen.setWidthF(self._scaled(design.tokens.Metric.STEP_RING_W))
        painter.setPen(ring_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(rect)

        painter.setPen(QPen(self.BADGE_TEXT_COLOUR))
        painter.setFont(self._font())
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(self.number))

    def hit_test(self, point: QPointF) -> bool:
        # RADIUS (tokens.py's STEP_D / 2) through `_scaled`, not
        # stroke-derived -- mirrors draw()'s own sizing, and the badge's
        # filled area is already generous enough that no extra
        # HIT_TOLERANCE is needed.
        radius = self._scaled(self.RADIUS)
        path = QPainterPath()
        path.addEllipse(self.point, radius, radius)
        return path.contains(point)


def next_step_number(shapes: list[Shape]) -> int:
    """The number a newly placed `StepMarker` should get: one higher than
    how many `StepMarker`s are already in `shapes`.

    Callers (`Canvas.mousePressEvent` in editor.py) assign this once, at
    creation, and never touch `.number` again — see `StepMarker`'s own
    docstring for why that is what keeps surviving badges' numbers stable
    after an earlier one is deleted.
    """
    return sum(1 for shape in shapes if isinstance(shape, StepMarker)) + 1


def render(base_image: QImage, shapes: list[Shape]) -> QImage:
    """Flatten `shapes` onto a copy of `base_image`, in list order.

    `QImage(base_image)` copies via Qt's implicit sharing, so this is cheap
    until the QPainter below actually writes to it (copy-on-write) — which
    is also what makes "never mutates base_image" hold. The QPainter is
    closed (`.end()`) before the copy is returned, per CLAUDE.md's rule
    against reading a pixmap while a QPainter is still open on it.

    StepMarker numbers are assigned once, by `next_step_number()`, at the
    point a badge is created — this function never touches `.number`, just
    paints whatever each StepMarker already carries. That is what makes
    "delete step 2, and 1/3 keep their own numbers" hold: there is no
    list-order recomputation here that could disagree with a badge's
    original number. See StepMarker's own docstring.

    An `ObscuringShape` (`Blur`/`Pixelate`/`Redact`) reads already-rendered pixels
    rather than painting onto a painter, so it gets different treatment
    here: the active painter is closed before `apply()` runs (making every
    shape drawn earlier in the list visible to it, since a painter left
    open on `result` isn't guaranteed to have flushed pending strokes where
    a plain pixel read would see them) and a fresh painter is reopened
    afterwards for whatever shapes follow. This is the core of this
    ticket's ordering guarantee — see shapes.py's module docstring and
    CLAUDE.md.

    `Spotlight` is the one `ObscuringShape` this loop does not call
    `apply()` on individually: every `Spotlight` in `shapes` is collected
    up front and flattened in one `Spotlight.apply_all()` call, the first
    time any of them is reached, so several spotlights combine into a
    single dim with several holes rather than each redimming the last
    one's (see `Spotlight`'s own docstring). Later spotlights in the list
    are then skipped as already accounted for.
    """
    result = QImage(base_image)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    spotlights = [shape for shape in shapes if isinstance(shape, Spotlight)]
    spotlights_done = False
    # try/finally, not a plain sequence: one shape raising out of draw() --
    # ObscuringShape.draw() does so by design, and a shape built by replace()
    # can dodge _check_style -- would otherwise leave this painter open on
    # `result`, and the caller reads `result` straight back. CLAUDE.md names
    # that as the rule which has already cost one bug: a pixmap read while a
    # painter is still active is not guaranteed to show what was painted.
    try:
        for shape in shapes:
            if isinstance(shape, Spotlight):
                if spotlights_done:
                    continue
                painter.end()
                result = Spotlight.apply_all(result, spotlights)
                painter = QPainter(result)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                spotlights_done = True
                continue
            if isinstance(shape, ObscuringShape):
                painter.end()
                result = shape.apply(result)
                painter = QPainter(result)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                continue
            shape.draw(painter)
    finally:
        # `painter` is rebound by the branches above, so this ends whichever
        # one is live -- and isActive() because those branches end it before
        # rebinding, so an exception between the two leaves nothing to close.
        if painter.isActive():
            painter.end()
    return result


def apply_crop(frame: Frame, shapes: list[Shape], crop_rect: QRectF) -> Frame:
    """Flatten `shapes` onto `frame.image`, then replace it outright with
    the pixels inside `crop_rect` (image-pixel coordinates, already
    normalized by the caller).

    Unlike every other shape, cropping isn't appended to a persistent shape
    list — it flattens what exists (so an annotation half-covering the crop
    region survives inside it) and produces a new base `Frame`, which is
    why this is a plain function rather than a method on `Crop`. Callers
    (Canvas.mouseReleaseEvent) are expected to clear their shape list after
    calling this, since it's now baked into the returned Frame's image.
    """
    flattened = render(frame.image, shapes)

    pixel_rect = _rounded_pixel_rect(crop_rect).intersected(flattened.rect())
    if pixel_rect.width() <= 0 or pixel_rect.height() <= 0:
        # QImage.copy() treats a null (0x0) QRect as "copy the whole
        # image" rather than "copy nothing" — not what a degenerate crop
        # means here, so this is constructed directly instead.
        cropped_image = QImage()
    else:
        cropped_image = flattened.copy(pixel_rect)

    # Inverts Frame.crop()'s own pixels-per-logical-unit scaling
    # (capture.py, scale_x/scale_y) rather than re-deriving it, so a future
    # fix to that scaling math only has to land in one place.
    scale_x = frame.image.width() / frame.logical_size.width()
    scale_y = frame.image.height() / frame.logical_size.height()
    new_logical_origin = frame.logical_origin + QPointF(
        pixel_rect.x() / scale_x, pixel_rect.y() / scale_y
    )
    new_logical_size = QSizeF(
        pixel_rect.width() / scale_x, pixel_rect.height() / scale_y
    )

    return Frame(
        image=cropped_image,
        logical_origin=new_logical_origin,
        logical_size=new_logical_size,
    )


def _transformed(shape: Shape, map_point, length_scale: float = 1.0) -> Shape:
    """Return a copy of `shape` with every point passed through `map_point`
    (a `QPointF -> QPointF` callable), and every length it paints with
    stretched by `length_scale`, the factor `map_point` stretches distances
    by (see `Shape._scaled`). `shape` itself is left untouched.

    Dispatches on field name rather than shape type: every shape class ink
    can be made of stores its geometry under one of exactly three names —
    `points` (Pen/Highlighter), `start`/`end` (Line/Arrow/Rectangle/Ellipse/
    ObscuringShape/Crop) or `point` (Text/StepMarker) — so a future shape
    class needs no matching update here as long as it reuses one of those
    names, which every existing one already does. Lengths need no such
    convention: `length_scale` lives on `Shape` itself. A snapped
    highlighter's `bands` are the one geometry beside `points`, and move
    corner by corner with them.
    """
    shape = replace(shape, length_scale=shape.length_scale * length_scale)
    if hasattr(shape, "points"):
        moved = replace(shape, points=[map_point(point) for point in shape.points])
        if getattr(moved, "bands", None):
            moved = replace(moved, bands=[
                QRectF(map_point(band.topLeft()), map_point(band.bottomRight()))
                for band in moved.bands
            ])
        return moved
    if hasattr(shape, "start") and hasattr(shape, "end"):
        return replace(shape, start=map_point(shape.start), end=map_point(shape.end))
    if hasattr(shape, "point"):
        return replace(shape, point=map_point(shape.point))
    raise TypeError(f"don't know how to translate a {type(shape).__name__}")


# The watermark (#69) is not a mark. Nobody draws it, the undo stack and
# the eraser never see it, and it is in no list of shapes: it is laid over
# the whole capture once, above every mark, by whatever paints the capture --
# the overlay's live preview, and `render_selection`'s export.
_WATERMARK_CORNERS = frozenset(corner for corner, _name in design.tokens.WATERMARK_CORNERS)

# Resolved on first use rather than per paint: the overlay repaints at the
# marching ants' rate, and asking the font database for every family it has
# thirty times a second would be most of the cost of a preview.
_watermark_family: str | None = None


def _watermark_font(logical_px: float, scale: float, family: str = "") -> QFont:
    """The text mark's type at `logical_px`, drawn `scale` painter pixels to
    a logical one, in `family` -- or the app's UI font when that is "".
    Measuring and painting both come through here, so a family Qt has to
    substitute for is substituted the same way for both.

    Whole logical pixels first, and only then scaled, as `Text._font` does:
    rounding once in each space keeps the export's type the screen's at the
    crop's ratio, rather than two sizes rounded apart.
    """
    global _watermark_family
    if _watermark_family is None:
        _watermark_family = design.font_families().ui
    metric = design.tokens.WatermarkMetric
    font = QFont(family or _watermark_family)
    font.setPixelSize(max(1, round(max(1, round(logical_px)) * scale)))
    font.setWeight(QFont.Weight(design.tokens.WatermarkFont.MARK_WEIGHT))
    font.setLetterSpacing(
        QFont.SpacingType.AbsoluteSpacing, metric.TEXT_TRACKING * font.pixelSize()
    )
    return font


@dataclass(frozen=True)
class Watermark:
    """A mark stamped once on a whole capture: a line of `text`, or an
    `image`, in one `corner`, at `opacity` percent.

    One rule places it for both of the things that paint it, so a preview
    and an export cannot disagree. The layout is worked out in logical
    pixels against the capture, and only then mapped into the space the
    painter is in: `scale` is the painter's pixels per logical pixel -- 1.0
    on the overlay, whose window coordinates are logical, and the crop's
    ratio on export, whose pixels are physical. The inset and every size go
    through it, so a mark exported from a 1.5x monitor covers one and a half
    times the pixels, exactly as its preview did on that monitor.

    Its height follows the capture (`WatermarkMetric.MARK_H_SHARE`, held
    between `MARK_H_MIN` and `MARK_H_MAX`) and it always fits inside the
    capture's insets. An image is drawn as it is, never stretched past its
    own pixels: that cap is counted in the frame's pixels, `pixel_ratio` of
    them to a logical pixel, whichever space the painter is in. On export
    that is `scale` again; the overlay passes its frame's ratio. Text is the
    spec's placeholder chip grown to the mark's height, elided if the
    capture is too narrow for it.
    """

    corner: str
    opacity: int
    text: str = ""
    image: QImage | None = None
    # The text mark's own look, from Settings. `color` None is the plate's
    # light type; `font_family` "" is the app's UI font. Without `backing`
    # there is no plate, and a thin halo in the opposite lightness keeps the
    # type readable over whatever the corner holds.
    color: QColor | None = None
    font_family: str = ""
    backing: bool = True

    def __post_init__(self) -> None:
        if self.corner not in _WATERMARK_CORNERS:
            raise ValueError(f"not a watermark corner: {self.corner!r}")
        low, high = design.tokens.WATERMARK["opacity_range"]
        if not low <= self.opacity <= high:
            raise ValueError(f"watermark opacity {self.opacity} is outside {low}-{high}")

    def rect(
        self, area: QRectF, scale: float = 1.0, pixel_ratio: float | None = None
    ) -> QRectF | None:
        """Where the mark lands on a capture covering `area`, in `area`'s own
        space -- or None where there is nothing to stamp, or no room inside
        the capture's insets to stamp it."""
        layout = self._layout(area, scale, pixel_ratio)
        return None if layout is None else layout[0]

    def paint(
        self,
        painter: QPainter,
        area: QRectF,
        scale: float = 1.0,
        pixel_ratio: float | None = None,
    ) -> None:
        """Paint the mark on a capture covering `area` -- see `rect`."""
        layout = self._layout(area, scale, pixel_ratio)
        if layout is None:
            return
        rect, text, grow = layout
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setOpacity(painter.opacity() * self.opacity / 100)
        if self.image is not None:
            painter.drawImage(rect, self.image)
        else:
            metric = design.tokens.WatermarkMetric
            font = _watermark_font(metric.TEXT_PX * grow, scale, self.font_family)
            ink = QColor(self.color) if self.color is not None else design.watermark_color("MARK_TEXT")
            if self.backing:
                radius = metric.TEXT_RADIUS * grow * scale
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(design.watermark_color("MARK_PLATE"))
                painter.drawRoundedRect(rect, radius, radius)
                pad_h = metric.TEXT_PAD[1] * grow * scale
                painter.setFont(font)
                painter.setPen(ink)
                # Not clipped to the chip: the text was measured at the
                # logical size, and whole-pixel type at another scale can
                # run a pixel wider than the chip scaled from it.
                painter.drawText(
                    rect.adjusted(pad_h, 0, -pad_h, 0),
                    int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextDontClip),
                    text,
                )
            else:
                self._paint_haloed_text(painter, rect, text, font, ink)
        painter.restore()

    @staticmethod
    def _paint_haloed_text(
        painter: QPainter, rect: QRectF, text: str, font: QFont, ink: QColor
    ) -> None:
        """`text` centred in `rect` with no plate: a halo in the opposite
        lightness under the fill, so light type still reads on a light
        corner and dark type on a dark one. As a path, because a stroke
        can only follow an outline, and the fill uses the same path so the
        two cannot drift apart.
        """
        metrics = QFontMetricsF(font)
        advance = metrics.horizontalAdvance(text)
        baseline = rect.center().y() + (metrics.ascent() - metrics.descent()) / 2
        path = QPainterPath()
        path.addText(QPointF(rect.center().x() - advance / 2, baseline), font, text)
        halo = design.watermark_color(
            "MARK_HALO_DARK" if ink.lightnessF() > 0.5 else "MARK_HALO_LIGHT"
        )
        width = max(1.0, font.pixelSize() * design.tokens.WatermarkMetric.HALO_SHARE)
        pen = QPen(halo, width)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.strokePath(path, pen)
        painter.fillPath(path, ink)

    def _layout(
        self, area: QRectF, scale: float, pixel_ratio: float | None
    ) -> tuple[QRectF, str, float] | None:
        """`(rect in area's space, the text as it fits, how far the text
        chip grew)`, or None -- see `rect`."""
        if scale <= 0:
            return None
        tokens = design.tokens
        metric = tokens.WatermarkMetric
        inset = tokens.WATERMARK["inset"]

        # The capture, and the room inside its insets: logical pixels.
        capture_w = area.width() / scale
        capture_h = area.height() / scale
        room_w = capture_w - 2 * inset
        room_h = capture_h - 2 * inset
        if room_w <= 0 or room_h <= 0:
            return None
        height = max(
            metric.MARK_H_MIN,
            min(min(capture_w, capture_h) * metric.MARK_H_SHARE, metric.MARK_H_MAX),
        )

        text, grow = "", 1.0
        if self.image is not None:
            if self.image.isNull():
                return None
            natural_w, natural_h = self.image.width(), self.image.height()
            height = min(height, natural_h / (pixel_ratio or scale))
            width = height * natural_w / natural_h
            widest = min(capture_w * metric.MARK_W_SHARE, room_w)
            if width > widest:
                height *= widest / width
                width = widest
            if height > room_h:
                width *= room_h / height
                height = room_h
        else:
            if not self.text:
                return None
            height = min(height, room_h)
            pad_v, pad_h = metric.TEXT_PAD
            grow = height / (metric.TEXT_PX + 2 * pad_v)
            metrics = QFontMetricsF(_watermark_font(metric.TEXT_PX * grow, 1.0, self.font_family))
            pad = pad_h * grow
            text = metrics.elidedText(self.text, Qt.TextElideMode.ElideRight, room_w - 2 * pad)
            if not text:
                return None
            width = min(metrics.horizontalAdvance(text) + 2 * pad, room_w)

        left = inset if self.corner in ("tl", "bl") else capture_w - inset - width
        top = inset if self.corner in ("tl", "tr") else capture_h - inset - height
        rect = QRectF(
            area.x() + left * scale, area.y() + top * scale, width * scale, height * scale
        )
        return rect, text, grow


def render_selection(
    frame: Frame,
    shapes: list[Shape],
    selection: QRectF,
    watermark: Watermark | None = None,
) -> QImage:
    """Export the annotated selection as a flattened `QImage`.

    `selection` and every point in `shapes` are in overlay-window
    coordinates — local to `frame`'s own top-left, the same space
    `OverlayWindow` keeps its selection and ink layer in (see this module's
    docstring) — never in the absolute virtual-desktop space
    `frame.logical_origin`/`Frame.crop()` use, or in `frame.image`'s own
    pixel space, which can differ from both under display scaling.

    This is the one translation docs/design/overlay-redesign.md's "Ink
    lives in screen coordinates" describes ("Export then translates by the
    selection origin once, at the point the image is produced"): `selection`
    is offset back onto `frame.logical_origin` for `Frame.crop()`'s sake,
    then every mark is shifted by `-selection`'s own origin and scaled by
    the crop's own image-pixels-per-logical-unit ratio — the same ratio
    `Frame.crop()` derives internally, and every length it paints with by
    the same ratio (see `Shape._scaled`) — before `render()` flattens them
    onto the cropped pixels. A mark whose points land outside the cropped
    image's bounds is simply never painted there, which is what keeps this
    consistent with the live ink layer's clip-rect behaviour without this
    function needing its own explicit clip.

    `watermark`, when there is one, is stamped last, once, above every mark
    and over the whole crop, at the same ratio as every mark's lengths --
    its inset and size are logical pixels too. It never joins `shapes`, so
    nothing that edits marks can reach it.
    """
    cropped = frame.crop(selection.translated(frame.logical_origin))

    logical_width = cropped.logical_size.width()
    logical_height = cropped.logical_size.height()
    scale_x = cropped.image.width() / logical_width if logical_width else 1.0
    scale_y = cropped.image.height() / logical_height if logical_height else 1.0
    origin = selection.topLeft()

    def to_cropped_pixel(point: QPointF) -> QPointF:
        local = point - origin
        return QPointF(local.x() * scale_x, local.y() * scale_y)

    # Image pixels per logical pixel, for lengths. A length has no axis -- a
    # stroke's width runs across it, a dash along it, a badge's radius every
    # way -- so it takes one ratio for both; the two differ only by the
    # crop's rounding. A length left out of it exports smaller than the
    # screen drew it by this ratio, and a dash left out repeats more often
    # along its edge, a different pattern from the screen's. An overlay
    # selection is whole logical pixels, so at 1x the crop is exactly as
    # many image pixels and this is exactly 1.0: nothing painted there moves.
    length_scale = (scale_x + scale_y) / 2
    mapped_shapes = [_transformed(shape, to_cropped_pixel, length_scale) for shape in shapes]
    result = render(cropped.image, mapped_shapes)
    if watermark is not None:
        painter = QPainter(result)
        watermark.paint(painter, QRectF(result.rect()), length_scale)
        painter.end()
    return result
