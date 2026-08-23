"""Annotation data model and the flattening renderer.

Per CLAUDE.md, annotations are kept as data — never painted directly and
destructively onto a live canvas pixmap — and `render()` flattens an ordered
shape list onto a *copy* of the base image. This buys exact undo, non-
destructive editing, and correct blur/pixelate later, and it is what lets the
same shape list back both the editor's live preview and, eventually, the
final saved image.

Every coordinate stored on a `Shape` is in **image-pixel** coordinates — the
same space `Canvas.widget_to_image` produces — never widget-local or logical
screen coordinates. Per CLAUDE.md's coordinate-space convention, this is
stated once here rather than re-derived per shape.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from PyQt6.QtCore import Qt, QPointF, QRectF
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen, QPolygonF


@dataclass
class Shape(ABC):
    """Base of the annotation data model. Coordinates are image-pixel space.

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
    """
    result = QImage(base_image)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    step_counter = 0
    for shape in shapes:
        if isinstance(shape, StepMarker):
            step_counter += 1
            shape.number = step_counter
        shape.draw(painter)
    painter.end()
    return result
