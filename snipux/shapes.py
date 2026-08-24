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
back to image-pixel space, once, at export. Per CLAUDE.md's coordinate-space
convention, each caller's choice is stated once here rather than re-derived
per shape.
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

    def _pen(self) -> QPen:
        pen = QPen(self.colour)
        pen.setWidthF(self.stroke_width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        return pen

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

    def _pen(self) -> QPen:
        pen = super()._pen()
        pen.setWidthF(self.stroke_width * design.tokens.Metric.HIGHLIGHT_MULT)
        return pen

    def draw(self, painter: QPainter) -> None:
        if len(self.points) < 2:
            return
        painter.setPen(self._pen())
        painter.setOpacity(design.tokens.Metric.HIGHLIGHT_ALPHA)
        painter.drawPolyline(QPolygonF(self.points))
        painter.setOpacity(1.0)

    def hit_test(self, point: QPointF) -> bool:
        # Reuses this class's own _pen() override (stroke x HIGHLIGHT_MULT)
        # via _stroke_hit_test, so the hit region matches the wider stroke
        # actually painted, not the narrower Pen-sized one.
        path = _polyline_path(self.points)
        return path is not None and self._stroke_hit_test(path, point)


@dataclass
class Line(Shape):
    """A straight stroke between two image-pixel points."""

    start: QPointF = field(default_factory=QPointF)
    end: QPointF = field(default_factory=QPointF)

    def draw(self, painter: QPainter) -> None:
        painter.setPen(self._pen())
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
    """

    HEAD_LENGTH_MIN = 10.0
    HEAD_LENGTH_FACTOR = 3.4
    HEAD_HALF_WIDTH_MIN = 7.0
    HEAD_HALF_WIDTH_FACTOR = 2.2
    SHAFT_STOP_FRACTION = 0.55

    start: QPointF = field(default_factory=QPointF)
    end: QPointF = field(default_factory=QPointF)

    def draw(self, painter: QPainter) -> None:
        dx = self.end.x() - self.start.x()
        dy = self.end.y() - self.start.y()
        shaft_length = math.hypot(dx, dy)

        painter.setPen(self._pen())
        if shaft_length == 0:
            painter.drawLine(self.start, self.end)  # degenerate arrow: a dot
            return

        head_length = max(self.HEAD_LENGTH_MIN, self.stroke_width * self.HEAD_LENGTH_FACTOR)
        head_half_width = max(
            self.HEAD_HALF_WIDTH_MIN, self.stroke_width * self.HEAD_HALF_WIDTH_FACTOR
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
    """An unfilled, stroke-only rounded rectangle spanning two image-pixel
    corners, per docs/design/overlay-redesign.md's "Drawing" (3px corner
    radius). `finalize_mark()` below is what normalises a negative
    width/height on release; draw() itself already tolerates either corner
    order via `_rect_from_corners`, for the live in-progress preview.
    """

    CORNER_RADIUS = 3.0

    start: QPointF = field(default_factory=QPointF)
    end: QPointF = field(default_factory=QPointF)

    def draw(self, painter: QPainter) -> None:
        painter.setPen(self._pen())
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(
            _rect_from_corners(self.start, self.end), self.CORNER_RADIUS, self.CORNER_RADIUS
        )

    def hit_test(self, point: QPointF) -> bool:
        # The stroked outline only -- clicking the empty, unfilled
        # interior is clicking whatever is behind the rectangle, not the
        # rectangle itself, same reasoning editor.py's own hit-testing
        # gives for the analogous case.
        path = QPainterPath()
        path.addRect(_rect_from_corners(self.start, self.end))
        return self._stroke_hit_test(path, point)


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
    - `Arrow`/`Rectangle`/`ObscuringShape` (`Blur`/`Pixelate`) need their
      bounding box to exceed `DROP_THRESHOLD` in at least one axis -- a
      horizontal or vertical drag is a deliberate mark even though it is
      exactly zero in the other axis, so this is an *or*, not an *and*.

    `Rectangle` additionally gets its `start`/`end` normalised to
    top-left/bottom-right order here, independent of the size check --
    `draw()` already tolerates either order for the live preview (see its
    docstring), but the *committed* shape needs a stable convention for
    later callers (eraser hit-testing, `_transformed`) the way every other
    two-point shape already has by construction. `ObscuringShape` needs no
    such normalising: `apply()` already runs its own corners through
    `_rect_from_corners` internally, so a raw start/end pair -- release
    ahead of press or not -- is already what it expects.

    Every other shape (`Line`, `Text`, `StepMarker`, `Crop`) has no
    drag-size or corner-order notion this ticket scopes, so it is returned
    unchanged.
    """
    if isinstance(shape, (Pen, Highlighter)):
        return shape if len(shape.points) > 1 else None

    if isinstance(shape, (Arrow, Rectangle, ObscuringShape)):
        rect = _rect_from_corners(shape.start, shape.end)
        if rect.width() <= DROP_THRESHOLD and rect.height() <= DROP_THRESHOLD:
            return None
        if isinstance(shape, Rectangle):
            return replace(shape, start=rect.topLeft(), end=rect.bottomRight())
        return shape

    return shape


@dataclass
class Ellipse(Shape):
    """An unfilled, stroke-only ellipse bounded by two image-pixel corners."""

    start: QPointF = field(default_factory=QPointF)
    end: QPointF = field(default_factory=QPointF)

    def draw(self, painter: QPainter) -> None:
        painter.setPen(self._pen())
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(_rect_from_corners(self.start, self.end))

    def hit_test(self, point: QPointF) -> bool:
        # Stroked outline only -- same "interior isn't the shape" reasoning
        # as Rectangle.hit_test above.
        path = QPainterPath()
        path.addEllipse(_rect_from_corners(self.start, self.end))
        return self._stroke_hit_test(path, point)


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
class Crop(Shape):
    """Live marquee for the crop tool: a dashed, unfilled rectangle.

    Unlike every other two-point shape, a `Crop` is never appended to a
    persistent shape list and render() never sees a committed one — it
    only ever exists as Canvas's transient in-progress shape during a
    drag, purely so the drag gets the same live-preview path as
    Rectangle/Ellipse/etc. The actual crop is performed by `apply_crop()`
    once the drag ends, which flattens and replaces the base image instead
    of adding to the shape list — see its docstring.
    """

    start: QPointF = field(default_factory=QPointF)
    end: QPointF = field(default_factory=QPointF)

    def draw(self, painter: QPainter) -> None:
        pen = self._pen()
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(_rect_from_corners(self.start, self.end))


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
    scales.
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
        font = QFont()
        font.setPixelSize(
            max(
                self.TEXT_FONT_SIZE_MIN,
                round(self.stroke_width * self.TEXT_FONT_SIZE_FACTOR),
            )
        )
        return font

    def draw(self, painter: QPainter) -> None:
        if not self.text:
            return  # an empty string is a no-op, not an error: see PLAN.md

        font = self._font()
        metrics = QFontMetricsF(font)
        pad_h = design.tokens.Metric.TEXT_LABEL_PAD_H
        pad_v = design.tokens.Metric.TEXT_LABEL_PAD_V
        chip = QRectF(
            self.point.x(),
            self.point.y(),
            metrics.horizontalAdvance(self.text) + pad_h * 2,
            metrics.height() + pad_v * 2,
        )
        radius = design.tokens.Metric.TEXT_LABEL_RADIUS

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
        ring_pen.setWidthF(design.tokens.Metric.TEXT_LABEL_RING_W)
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
        # A generous fixed box centred on the anchor point rather than
        # measuring the actual chip draw() paints -- good enough to pick
        # the label out without duplicating this class's own font-metrics
        # sizing logic here, same spirit as its `_font()`/chip layout.
        half_extent = self.HIT_TOLERANCE + self.stroke_width * self.TEXT_FONT_SIZE_FACTOR
        rect = QRectF(
            self.point.x() - half_extent,
            self.point.y() - half_extent,
            half_extent * 2,
            half_extent * 2,
        )
        return rect.contains(point)


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
    reading experience.
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
        font.setPixelSize(round(size))
        font.setWeight(QFont.Weight(weight))
        return font

    def _rect(self) -> QRectF:
        radius = self.RADIUS
        return QRectF(
            self.point.x() - radius, self.point.y() - radius, radius * 2, radius * 2
        )

    def _draw_shadow(self, painter: QPainter, rect: QRectF) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        for layer in range(self._SHADOW_LAYERS, 0, -1):
            # Outermost layer first (biggest, faintest) so each inner layer
            # paints over it rather than the reverse -- otherwise a fainter
            # outer ellipse would be visible on top of a stronger inner one.
            grown = self._SHADOW_SPREAD * layer / self._SHADOW_LAYERS
            alpha = self._SHADOW_MAX_ALPHA * (1 - (layer - 1) / self._SHADOW_LAYERS)
            shadow_colour = QColor(0, 0, 0)
            shadow_colour.setAlphaF(alpha)
            painter.setBrush(shadow_colour)
            painter.drawEllipse(
                rect.adjusted(-grown, -grown, grown, grown).translated(
                    0, self._SHADOW_OFFSET_Y
                )
            )

    def draw(self, painter: QPainter) -> None:
        rect = self._rect()

        self._draw_shadow(painter, rect)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.colour)
        painter.drawEllipse(rect)

        ring_pen = QPen(QColor(design.tokens.Color.STEP_RING))
        ring_pen.setWidthF(design.tokens.Metric.STEP_RING_W)
        painter.setPen(ring_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(rect)

        painter.setPen(QPen(self.BADGE_TEXT_COLOUR))
        painter.setFont(self._font())
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(self.number))

    def hit_test(self, point: QPointF) -> bool:
        # Fixed at RADIUS (tokens.py's STEP_D / 2), not stroke-derived --
        # mirrors draw()'s own sizing, and the badge's filled area is
        # already generous enough that no extra HIT_TOLERANCE is needed.
        path = QPainterPath()
        path.addEllipse(self.point, self.RADIUS, self.RADIUS)
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

    An `ObscuringShape` (`Blur`/`Pixelate`) samples already-rendered pixels
    rather than painting onto a painter, so it gets different treatment
    here: the active painter is closed before `apply()` runs (making every
    shape drawn earlier in the list visible to it, since a painter left
    open on `result` isn't guaranteed to have flushed pending strokes where
    a plain pixel read would see them) and a fresh painter is reopened
    afterwards for whatever shapes follow. This is the core of this
    ticket's ordering guarantee — see shapes.py's module docstring and
    CLAUDE.md.
    """
    result = QImage(base_image)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    for shape in shapes:
        if isinstance(shape, ObscuringShape):
            painter.end()
            result = shape.apply(result)
            painter = QPainter(result)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            continue
        shape.draw(painter)
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


def _transformed(shape: Shape, map_point) -> Shape:
    """Return a copy of `shape` with every point passed through `map_point`
    (a `QPointF -> QPointF` callable). `shape` itself is left untouched.

    Dispatches on field name rather than shape type: every shape class ink
    can be made of stores its geometry under one of exactly three names —
    `points` (Pen/Highlighter), `start`/`end` (Line/Arrow/Rectangle/Ellipse/
    ObscuringShape/Crop) or `point` (Text/StepMarker) — so a future shape
    class needs no matching update here as long as it reuses one of those
    names, which every existing one already does.
    """
    if hasattr(shape, "points"):
        return replace(shape, points=[map_point(point) for point in shape.points])
    if hasattr(shape, "start") and hasattr(shape, "end"):
        return replace(shape, start=map_point(shape.start), end=map_point(shape.end))
    if hasattr(shape, "point"):
        return replace(shape, point=map_point(shape.point))
    raise TypeError(f"don't know how to translate a {type(shape).__name__}")


def _mask_outside_path(image: QImage, path: QPainterPath) -> QImage:
    """Return a copy of `image` (already in `image`'s own pixel coordinates,
    same space as `path`) with every pixel outside `path` forced fully
    transparent and everything inside it left untouched.

    Per docs/design/overlay-redesign.md's "Capture modes" entry for
    Freeform: "export crops to its bounding box with the outside
    transparent." Built from a same-size mask image — opaque wherever
    `path` fills, fully transparent everywhere else — composited via
    `QPainter.CompositionMode.CompositionMode_DestinationIn`, rather than a
    per-pixel loop: `drawImage()` rasterizes across the mask's *entire*
    rect regardless of any given pixel's own alpha, so the composition
    zeroes out-of-path destination pixels uniformly across the whole image
    — a `fillPath` directly on `image` would only touch the pixels the path
    itself covers and leave everything outside it exactly as `image`
    already had it.

    Converts to an alpha-carrying format first: the frame this is normally
    called on is `Format_RGB32` (see capture.py), which has no alpha
    channel to punch a hole in at all.
    """
    result = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)

    mask = QImage(result.size(), QImage.Format.Format_ARGB32_Premultiplied)
    mask.fill(Qt.GlobalColor.transparent)
    mask_painter = QPainter(mask)
    mask_painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    mask_painter.setPen(Qt.PenStyle.NoPen)
    mask_painter.setBrush(Qt.GlobalColor.black)  # opaque; only its alpha is read below
    mask_painter.drawPath(path)
    mask_painter.end()  # closed before `mask` is read via drawImage, per CLAUDE.md

    painter = QPainter(result)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
    painter.drawImage(0, 0, mask)
    painter.end()  # closed before this function returns `result`, per CLAUDE.md
    return result


def render_selection(
    frame: Frame,
    shapes: list[Shape],
    selection: QRectF,
    selection_path: QPainterPath | None = None,
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
    `Frame.crop()` derives internally — before `render()` flattens them onto
    the cropped pixels. A mark whose points land outside the cropped image's
    bounds is simply never painted there, which is what keeps this
    consistent with the live ink layer's clip-rect behaviour without this
    function needing its own explicit clip.

    `selection_path`, given only for a confirmed Freeform lasso (same
    coordinate space as `selection`/`shapes`), is put through the exact same
    origin-shift-then-scale mapping as every mark above — via `QTransform`
    rather than `_transformed`'s per-shape dispatch, since a `QPainterPath`
    isn't a `Shape` — and then handed to `_mask_outside_path` to force
    everything outside the lasso's own outline transparent. `None` (every
    other capture mode) skips this entirely, leaving the cropped rectangle
    opaque exactly as before this parameter existed.
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

    mapped_shapes = [_transformed(shape, to_cropped_pixel) for shape in shapes]
    image = render(cropped.image, mapped_shapes)

    if selection_path is not None:
        path_transform = QTransform(
            scale_x, 0, 0, scale_y, -origin.x() * scale_x, -origin.y() * scale_y
        )
        image = _mask_outside_path(image, path_transform.map(selection_path))

    return image
