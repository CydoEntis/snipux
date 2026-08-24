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
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen, QPolygonF

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

    @abstractmethod
    def draw(self, painter: QPainter) -> None:
        """Paint this shape onto `painter`'s active device."""

    def _pen(self) -> QPen:
        pen = QPen(self.colour)
        pen.setWidthF(self.stroke_width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        return pen


def _rect_from_corners(start: QPointF, end: QPointF) -> QRectF:
    return QRectF(start, end).normalized()


@dataclass
class Pen(Shape):
    """Freehand stroke, fully opaque. An ordered list of image-pixel points."""

    points: list[QPointF] = field(default_factory=list)

    def draw(self, painter: QPainter) -> None:
        if len(self.points) < 2:
            return
        painter.setPen(self._pen())
        painter.drawPolyline(QPolygonF(self.points))


@dataclass
class Highlighter(Shape):
    """Freehand stroke at reduced opacity, unlike `Pen`.

    Sets/restores opacity immediately around its own draw() so opacity never
    bleeds into shapes drawn after it in the same render() pass.
    """

    OPACITY = 0.4

    points: list[QPointF] = field(default_factory=list)

    def draw(self, painter: QPainter) -> None:
        if len(self.points) < 2:
            return
        painter.setPen(self._pen())
        painter.setOpacity(self.OPACITY)
        painter.drawPolyline(QPolygonF(self.points))
        painter.setOpacity(1.0)


@dataclass
class Line(Shape):
    """A straight stroke between two image-pixel points."""

    start: QPointF = field(default_factory=QPointF)
    end: QPointF = field(default_factory=QPointF)

    def draw(self, painter: QPainter) -> None:
        painter.setPen(self._pen())
        painter.drawLine(self.start, self.end)


@dataclass
class Arrow(Shape):
    """A straight shaft plus a filled arrowhead at `end`.

    Pixel-distinguishable from `Line` between the same two points: the
    arrowhead flares out beyond the shaft's own width near `end`.
    """

    # Arrowhead size scales with stroke width so it stays visible at any
    # width but never dwarfs a thin stroke or hides inside a thick one.
    HEAD_LENGTH_FACTOR = 3.0
    HEAD_ANGLE_RADIANS = math.radians(25)

    start: QPointF = field(default_factory=QPointF)
    end: QPointF = field(default_factory=QPointF)

    def draw(self, painter: QPainter) -> None:
        painter.setPen(self._pen())
        painter.drawLine(self.start, self.end)

        dx = self.end.x() - self.start.x()
        dy = self.end.y() - self.start.y()
        shaft_length = math.hypot(dx, dy)
        if shaft_length == 0:
            return  # degenerate arrow (start == end): shaft alone already drawn (a dot)

        angle = math.atan2(dy, dx)
        head_length = max(self.stroke_width, 1.0) * self.HEAD_LENGTH_FACTOR

        back_left = QPointF(
            self.end.x() - head_length * math.cos(angle - self.HEAD_ANGLE_RADIANS),
            self.end.y() - head_length * math.sin(angle - self.HEAD_ANGLE_RADIANS),
        )
        back_right = QPointF(
            self.end.x() - head_length * math.cos(angle + self.HEAD_ANGLE_RADIANS),
            self.end.y() - head_length * math.sin(angle + self.HEAD_ANGLE_RADIANS),
        )

        head = QPainterPath()
        head.moveTo(self.end)
        head.lineTo(back_left)
        head.lineTo(back_right)
        head.closeSubpath()
        painter.fillPath(head, self.colour)


@dataclass
class Rectangle(Shape):
    """An unfilled, stroke-only rectangle spanning two image-pixel corners."""

    start: QPointF = field(default_factory=QPointF)
    end: QPointF = field(default_factory=QPointF)

    def draw(self, painter: QPainter) -> None:
        painter.setPen(self._pen())
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(_rect_from_corners(self.start, self.end))


@dataclass
class Ellipse(Shape):
    """An unfilled, stroke-only ellipse bounded by two image-pixel corners."""

    start: QPointF = field(default_factory=QPointF)
    end: QPointF = field(default_factory=QPointF)

    def draw(self, painter: QPainter) -> None:
        painter.setPen(self._pen())
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(_rect_from_corners(self.start, self.end))


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
    """

    start: QPointF = field(default_factory=QPointF)
    end: QPointF = field(default_factory=QPointF)

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

        Obscures by downscaling the sampled patch then scaling it back up;
        the averaging a smooth downscale performs is what produces the
        blur/pixelate effect, so no manual convolution and no numpy/Pillow/
        OpenCV dependency, per CLAUDE.md.
        """
        pixel_rect = _clamped_pixel_rect(self.start, self.end, image)
        if pixel_rect is None:
            return image  # degenerate/out-of-bounds rect: no-op

        patch = image.copy(pixel_rect)
        small = patch.scaled(
            max(1, pixel_rect.width() // _OBSCURE_DOWNSCALE_DIVISOR),
            max(1, pixel_rect.height() // _OBSCURE_DOWNSCALE_DIVISOR),
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


# Sampled patch is downscaled to roughly this fraction before being scaled
# back up; the averaging a smooth downscale performs is what produces the
# blur, so the exact factor is an implementation detail, not a tuned
# constant anything depends on.
_OBSCURE_DOWNSCALE_DIVISOR = 8


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


MIN_PIXEL_SIZE = 8
FONT_SIZE_FACTOR = 4.0


def _font_for_stroke_width(stroke_width: float) -> QFont:
    """A `QFont` sized from `stroke_width`, floored so the default stroke
    width (3) never rounds down to an illegible or 0px font. Shared by
    `Text` and `StepMarker` — both derive glyph size from stroke width the
    same way, per PLAN.md ("any monotonic mapping with a floor is fine").
    """
    font = QFont()
    font.setPixelSize(max(MIN_PIXEL_SIZE, round(stroke_width * FONT_SIZE_FACTOR)))
    return font


@dataclass
class Text(Shape):
    """A string drawn at a single image-pixel point, in the shape's colour.

    Font size is derived from stroke_width rather than stored separately —
    there is no independent "text size" concept for this ticket, only the
    stroke-width control the toolbar already exposes for every other tool.
    """

    text: str = ""
    point: QPointF = field(default_factory=QPointF)

    def _font(self) -> QFont:
        return _font_for_stroke_width(self.stroke_width)

    def draw(self, painter: QPainter) -> None:
        if not self.text:
            return  # an empty string is a no-op, not an error: see PLAN.md
        painter.setPen(QPen(self.colour))
        painter.setFont(self._font())
        painter.drawText(self.point, self.text)


@dataclass
class StepMarker(Shape):
    """A filled numbered badge. `number` is assigned by render(), not at
    construction — see render()'s docstring for why.
    """

    RADIUS_FACTOR = 4.0
    MIN_RADIUS = 10
    BADGE_TEXT_COLOUR = QColor(Qt.GlobalColor.white)

    point: QPointF = field(default_factory=QPointF)
    number: int = 0

    def _font(self) -> QFont:
        return _font_for_stroke_width(self.stroke_width)

    def draw(self, painter: QPainter) -> None:
        radius = max(self.MIN_RADIUS, self.stroke_width * self.RADIUS_FACTOR)
        rect = QRectF(
            self.point.x() - radius,
            self.point.y() - radius,
            radius * 2,
            radius * 2,
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.colour)
        painter.drawEllipse(rect)

        painter.setPen(QPen(self.BADGE_TEXT_COLOUR))
        painter.setFont(self._font())
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(self.number))


def render(base_image: QImage, shapes: list[Shape]) -> QImage:
    """Flatten `shapes` onto a copy of `base_image`, in list order.

    `QImage(base_image)` copies via Qt's implicit sharing, so this is cheap
    until the QPainter below actually writes to it (copy-on-write) — which
    is also what makes "never mutates base_image" hold. The QPainter is
    closed (`.end()`) before the copy is returned, per CLAUDE.md's rule
    against reading a pixmap while a QPainter is still open on it.

    StepMarker numbers aren't stored durably; they're recomputed from list
    order on every call. That makes "renumber after an earlier one is
    removed" fall out for free — there is no stale "my number is N" to find
    and fix up, just whatever list order render() sees this time. This does
    mutate the StepMarker instances passed in, which is fine: the "never
    painted destructively" rule above is about not writing onto a live
    canvas pixmap, not about shape objects being immutable.

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
    step_counter = 0
    for shape in shapes:
        if isinstance(shape, StepMarker):
            step_counter += 1
            shape.number = step_counter
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


def render_selection(frame: Frame, shapes: list[Shape], selection: QRectF) -> QImage:
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
    return render(cropped.image, mapped_shapes)
