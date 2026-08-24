"""Frozen-frame selection windows, one per monitor.

Per CLAUDE.md's one architectural rule, the compositor is only ever asked
for pixels once (in `capture.py`). Everything here is ordinary painting and
mouse/key handling on the `Frame` that grab already produced — no code path
in this module asks for a fresh screen read while the user is dragging.

`Overlay` never re-zeroes a selection to its own monitor: every rect it
stores or emits is in absolute logical virtual-desktop coordinates, the same
space `Frame`/`monitor_geometry` use, which is what makes a selection
spanning two monitors arithmetic rather than a special case.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from enum import Enum
from pathlib import Path

from PyQt6.QtCore import Qt, QPoint, QPointF, QRect, QRectF, QSize, QSizeF, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QIcon,
    QImage,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QTransform,
)
from PyQt6.QtWidgets import (
    QApplication,
    QColorDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QWidget,
)

from snipux import design
from snipux.capture import Frame
from snipux.shapes import ObscuringShape, Shape, StepMarker, render_selection


class SelectionMode(Enum):
    """How the overlay turns mouse input into a selection.

    Chosen once before the overlay is shown (mirrors how the real app offers
    a mode picker before freezing the screen) — never switched mid-drag.
    """

    RECTANGLE = "rectangle"
    FREEFORM = "freeform"
    WINDOW = "window"
    FULL_SCREEN = "full_screen"


class GeometryProvider(ABC):
    """Source of per-window geometry for window-selection mode.

    X11 can answer this; Wayland compositors generally cannot, per
    CLAUDE.md's platform note. `is_available()` mirrors
    `CaptureBackend.is_available()` in capture.py so the two "can this
    platform do X" checks look the same everywhere they appear. It isn't
    load-bearing for the mode-3 fallback itself — a per-point `window_at()
    is None` already covers "no provider" and "no window here" alike — it's
    exposed so a future mode picker can grey out window mode instead of
    offering a dead option.
    """

    @abstractmethod
    def is_available(self) -> bool:
        """Whether this platform can report window geometry at all."""

    @abstractmethod
    def window_at(self, point: QPointF) -> QRectF | None:
        """Absolute logical rect of the window under `point`, or None."""


class UnsupportedGeometryProvider(GeometryProvider):
    """Default provider: reports no windows anywhere.

    This is what makes window mode degrade to plain rectangle dragging
    everywhere until a platform-specific provider (the X11 backend ticket)
    is wired in.
    """

    def is_available(self) -> bool:
        return False

    def window_at(self, point: QPointF) -> QRectF | None:
        return None


class Overlay(QWidget):
    """One frameless, always-on-top window covering a single monitor.

    Paints that monitor's slice of the frozen `Frame`, a dimmed veil outside
    the current selection, a live size readout, and a cursor-centered
    magnifier. Selection state and the `confirmed`/`cancelled` signals carry
    absolute logical virtual-desktop rects, never monitor-local ones.
    """

    # (bounds, exact_path_or_None). `object` carries the second slot because
    # it must hold either a QPainterPath (freeform) or None (every other
    # mode); PyQt passes arbitrary Python objects through `object`. A plain
    # rectangle/window/full-screen selection is never modelled as a
    # degenerate one-rectangle path — `None` says "this is just the bounds"
    # plainly, instead of making every consumer special-case it.
    confirmed = pyqtSignal(QRectF, object)
    cancelled = pyqtSignal()

    VEIL_COLOR = QColor(0, 0, 0, 120)
    CROSSHAIR_COLOR = QColor(255, 0, 0)

    # Source square is defined in *logical* px so it covers the same
    # real-world area regardless of the monitor's scale factor; only
    # converted to image pixels at crop time (see `_paint_magnifier`).
    MAGNIFIER_SOURCE_LOGICAL_SIZE = 20.0
    # Display box is a fixed logical size, independent of the source scale
    # factor — this is what keeps the crosshair centered on the same
    # logical pixel regardless of the monitor's scaling.
    MAGNIFIER_BOX_SIZE = 120
    # Offset from the cursor so the magnifier box never sits directly under
    # the pixel it is magnifying.
    MAGNIFIER_OFFSET = QPointF(20.0, 20.0)

    def __init__(
        self,
        frame: Frame,
        monitor_geometry: QRectF,
        parent=None,
        mode: SelectionMode = SelectionMode.RECTANGLE,
        geometry_provider: GeometryProvider | None = None,
        virtual_desktop_rect: QRectF | None = None,
    ):
        super().__init__(parent)
        self._monitor_geometry = QRectF(monitor_geometry)
        # Reuses Frame.crop()'s already-tested scaling/negative-origin
        # logic instead of re-deriving it here.
        self._monitor_frame = frame.crop(monitor_geometry)
        self._mode = mode
        self._geometry_provider = geometry_provider or UnsupportedGeometryProvider()

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setGeometry(
            round(monitor_geometry.x()),
            round(monitor_geometry.y()),
            round(monitor_geometry.width()),
            round(monitor_geometry.height()),
        )
        # Needed so mouseMoveEvent fires (for the magnifier) even when no
        # button is held, not just while dragging a selection.
        self.setMouseTracking(True)

        self._selection: QRectF | None = None
        # Local-logical cursor position, or None before the first move.
        self._cursor_pos: QPointF | None = None
        # Absolute-logical anchor of an in-progress left-button drag.
        self._drag_anchor: QPointF | None = None
        # In-progress freeform path, absolute logical coords. None outside
        # a freeform drag.
        self._drag_path: QPainterPath | None = None
        # Confirmed freeform path, the source of truth for "what pixels are
        # actually inside" once a freeform drag has been confirmed. None in
        # every other mode, and None during an in-progress freeform drag too
        # (that's `_drag_path`), so a stale confirmed path never lingers
        # across a new drag.
        self._selection_path: QPainterPath | None = None
        # Window rect a left-press landed on, remembered from press to
        # release in window mode — a window click is a click, not a drag,
        # for its entire duration, so this is captured once and only read.
        self._window_hit_rect: QRectF | None = None

        self._size_label = QLabel(self)
        self._size_label.setStyleSheet(
            "background-color: rgba(0, 0, 0, 160); color: white;"
            " padding: 2px 4px;"
        )
        self._size_label.hide()

        if self._mode is SelectionMode.FULL_SCREEN:
            # Full screen needs no drag: the whole virtual desktop (or, for
            # an overlay built directly without going through
            # create_overlays, this monitor alone) is selected from the
            # first paint. Goes through set_selection (not a raw attribute
            # write) so the label/veil are consistent immediately instead
            # of only catching up on the next unrelated repaint.
            self.set_selection(
                virtual_desktop_rect
                if virtual_desktop_rect is not None
                else self._monitor_geometry
            )

    # -- coordinate-space helpers -----------------------------------------
    # Every rect/point this widget touches is explicitly one of: absolute
    # logical (selection, monitor_geometry), local logical (post
    # _to_local/_to_absolute, used for painting/hit-testing), or — only for
    # the magnifier's source crop — local image-pixel. Naming follows suit.

    def _to_local(self, rect: QRectF) -> QRectF:
        """Absolute logical rect -> this widget's local logical rect."""
        origin = self._monitor_geometry.topLeft()
        return QRectF(rect.topLeft() - origin, rect.size())

    def _to_absolute(self, local_point: QPointF) -> QPointF:
        """Local logical point -> absolute logical point."""
        return local_point + self._monitor_geometry.topLeft()

    # -- public API ---------------------------------------------------------

    def set_selection(self, rect: QRectF | None) -> None:
        """Set the current selection (absolute logical coords) and repaint."""
        self._selection = rect
        self._update_size_label()
        self.update()

    # -- painting -------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.drawImage(QRectF(self.rect()), self._monitor_frame.image)
        self._paint_veil(painter)
        self._paint_magnifier(painter)
        painter.end()

    def _paint_veil(self, painter: QPainter) -> None:
        widget_rect = QRectF(self.rect())
        # A single even-odd fill dims everywhere except the selection hole
        # in one call, so there's no separate "dim then punch a hole" step
        # that could disagree with this one at the edge.
        path = QPainterPath()
        path.addRect(widget_rect)
        if self._selection is not None:
            local_selection = self._to_local(self._selection).intersected(widget_rect)
            if not local_selection.isEmpty():
                path.addRect(local_selection)
        path.setFillRule(Qt.FillRule.OddEvenFill)
        painter.fillPath(path, self.VEIL_COLOR)

    def _paint_magnifier(self, painter: QPainter) -> None:
        if self._cursor_pos is None:
            return
        image = self._monitor_frame.image
        logical_size = self._monitor_frame.logical_size
        if logical_size.width() <= 0 or logical_size.height() <= 0:
            return

        # Same per-axis scale Frame.crop() derives, applied here because the
        # magnifier reads .image pixels directly rather than letting
        # drawImage stretch implicitly the way the base layer does.
        scale_x = image.width() / logical_size.width()
        scale_y = image.height() / logical_size.height()

        image_cursor_x = self._cursor_pos.x() * scale_x
        image_cursor_y = self._cursor_pos.y() * scale_y

        half_width = (self.MAGNIFIER_SOURCE_LOGICAL_SIZE / 2) * scale_x
        half_height = (self.MAGNIFIER_SOURCE_LOGICAL_SIZE / 2) * scale_y

        width = min(round(half_width * 2), image.width())
        height = min(round(half_height * 2), image.height())
        if width <= 0 or height <= 0:
            return

        left = round(image_cursor_x - half_width)
        top = round(image_cursor_y - half_height)
        left = max(0, min(left, image.width() - width))
        top = max(0, min(top, image.height() - height))

        source_rect = QRect(left, top, width, height)
        cropped = image.copy(source_rect)
        zoomed = cropped.scaled(
            self.MAGNIFIER_BOX_SIZE,
            self.MAGNIFIER_BOX_SIZE,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            # Smoothing would hide the exact pixel this tool exists to show.
            Qt.TransformationMode.FastTransformation,
        )

        # Clamped into the widget the same way _update_size_label clamps its
        # label, so a cursor near a monitor's right/bottom edge still shows
        # a (repositioned) magnifier instead of one painted off-window and
        # clipped away entirely. The crosshair still marks the exact cursor
        # pixel: the source crop above is centered on the cursor regardless
        # of where the box itself ends up on screen.
        box_x = self._cursor_pos.x() + self.MAGNIFIER_OFFSET.x()
        box_y = self._cursor_pos.y() + self.MAGNIFIER_OFFSET.y()
        box_x = max(0.0, min(box_x, self.width() - self.MAGNIFIER_BOX_SIZE))
        box_y = max(0.0, min(box_y, self.height() - self.MAGNIFIER_BOX_SIZE))
        box_rect = QRectF(
            QPointF(box_x, box_y),
            QSizeF(self.MAGNIFIER_BOX_SIZE, self.MAGNIFIER_BOX_SIZE),
        )
        painter.drawImage(box_rect, zoomed)

        center = box_rect.center()
        painter.setPen(self.CROSSHAIR_COLOR)
        painter.drawLine(QPointF(box_rect.left(), center.y()), QPointF(box_rect.right(), center.y()))
        painter.drawLine(QPointF(center.x(), box_rect.top()), QPointF(center.x(), box_rect.bottom()))

    def _update_size_label(self) -> None:
        if self._selection is None:
            self._size_label.hide()
            return

        widget_rect = QRectF(self.rect())
        local_selection = self._to_local(self._selection)
        if local_selection.intersected(widget_rect).isEmpty():
            self._size_label.hide()
            return

        # Width/height come from the absolute selection, in logical pixels
        # per the acceptance criterion — never the (possibly larger)
        # image-pixel size.
        width = round(self._selection.width())
        height = round(self._selection.height())
        self._size_label.setText(f"{width} × {height}")
        self._size_label.adjustSize()

        label_x = round(local_selection.left())
        label_y = round(local_selection.top()) - self._size_label.height() - 4
        label_x = max(0, min(label_x, self.width() - self._size_label.width()))
        label_y = max(0, min(label_y, self.height() - self._size_label.height()))
        self._size_label.move(QPoint(label_x, label_y))
        self._size_label.show()

    # -- interaction ------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            # Immediate cancel, matching Escape's semantics; no drag starts,
            # in every mode.
            self.cancelled.emit()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return

        anchor = self._to_absolute(event.position())
        self._drag_anchor = anchor

        if self._mode is SelectionMode.FREEFORM:
            self._drag_path = QPainterPath()
            self._drag_path.moveTo(anchor)
            # Something to show from the very first pixel, same as
            # rectangle mode's live-drag feedback.
            self.set_selection(QRectF(anchor, QSizeF(0, 0)))
        elif self._mode is SelectionMode.WINDOW:
            self._window_hit_rect = self._geometry_provider.window_at(anchor)
            if self._window_hit_rect is not None:
                # Clicking a window highlights that window's full rect
                # before the button is even released.
                self.set_selection(self._window_hit_rect)
        # Rectangle / full screen: `_drag_anchor` alone is enough for their
        # release-time logic, nothing else to record here.

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._cursor_pos = event.position()

        if self._mode is SelectionMode.FULL_SCREEN:
            # Selection state is never touched here so the veil hole and
            # size label can't transiently shrink to a drag rect mid-move;
            # the whole desktop was already selected at construction time.
            self.update()
            return

        if self._mode is SelectionMode.FREEFORM:
            if self._drag_path is not None:
                self._drag_path.lineTo(self._to_absolute(event.position()))
                # Through set_selection, not a raw attribute write, so the
                # size label and veil hole live-update stroke by stroke.
                self.set_selection(self._drag_path.boundingRect())
            else:
                self.update()
            return

        if self._mode is SelectionMode.WINDOW:
            absolute_pos = self._to_absolute(event.position())
            if self._drag_anchor is not None and self._window_hit_rect is not None:
                # Press hit a window: a window click doesn't track the
                # mouse, the hit rect is already showing from press time.
                self.update()
            elif self._drag_anchor is not None:
                # Press missed every window: fall back to plain rectangle
                # tracking, per the acceptance criterion.
                self.set_selection(
                    QRectF(self._drag_anchor, absolute_pos).normalized()
                )
            else:
                # Plain hover: a miss actively clears any previously-shown
                # preview instead of leaving it stuck.
                self.set_selection(self._geometry_provider.window_at(absolute_pos))
            return

        # Rectangle.
        if self._drag_anchor is not None:
            absolute_pos = self._to_absolute(event.position())
            self.set_selection(QRectF(self._drag_anchor, absolute_pos).normalized())
        else:
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._drag_anchor is None:
            return

        anchor = self._drag_anchor
        self._drag_anchor = None
        absolute_pos = self._to_absolute(event.position())

        if self._mode is SelectionMode.FULL_SCREEN:
            # No distance/misfire check at all: any release confirms the
            # whole desktop, which was already selected at construction.
            self.confirmed.emit(self._selection, None)
            return

        if self._mode is SelectionMode.FREEFORM:
            path = self._drag_path
            self._drag_path = None
            # The release itself is a traced point, same as every
            # intermediate move — omitting it would silently drop the final
            # drag segment and let closeSubpath() cut straight from the
            # last *moved-to* point back to the anchor instead.
            path.lineTo(absolute_pos)
            path.closeSubpath()
            bounds = path.boundingRect()
            # Measured by the traced path's own bounding-rect diagonal, not
            # anchor-to-release distance: a closed-loop lasso back near its
            # start point has a large bounding-rect diagonal even though its
            # last pixel lands next to its first, so it isn't misfired away.
            diagonal = math.hypot(bounds.width(), bounds.height())
            if diagonal < QApplication.startDragDistance():
                self.set_selection(None)
                return
            self._selection_path = path
            self.set_selection(bounds)
            self.confirmed.emit(bounds, path)
            return

        if self._mode is SelectionMode.WINDOW and self._window_hit_rect is not None:
            rect = self._window_hit_rect
            self._window_hit_rect = None
            # Unconditional, no distance check, regardless of where the
            # release happened: a window click is a click, not a drag, for
            # its entire duration — the hit was captured at press time and
            # is only read here, never re-queried.
            self.set_selection(rect)
            self.confirmed.emit(rect, None)
            return

        if self._mode is SelectionMode.WINDOW:
            # No provider, or the press missed every window: fall through
            # to the same distance-threshold rectangle logic below, which
            # *is* the fallback the acceptance criterion asks for.
            self._window_hit_rect = None

        delta = absolute_pos - anchor
        distance = math.hypot(delta.x(), delta.y())

        if distance < QApplication.startDragDistance():
            # A press/release with no meaningful drag is a misfire per
            # SPEC.md, not a selection of nothing.
            self.set_selection(None)
            return

        rect = QRectF(anchor, absolute_pos).normalized()
        self.set_selection(rect)
        self.confirmed.emit(rect, None)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
        elif event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
            if self._selection is not None:
                # `_selection_path` is None except right after a freeform
                # drag has been confirmed by mouse release, so this carries
                # the exact shape for a completed freeform selection and
                # None for every other mode, same as a mouse-release confirm.
                self.confirmed.emit(self._selection, self._selection_path)
        else:
            super().keyPressEvent(event)


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


# ---------------------------------------------------------------------------
# Floating bar (SNX-40)
# ---------------------------------------------------------------------------
# docs/design/overlay-redesign.md's "Floating bar" section is the authority
# for every metric below; tokens.py is where each one actually lives. Two
# things that section calls out because they are easy to get wrong: the
# bar's fill is a 93%-alpha *paint*, never a 93%-*opacity* widget -- that
# would wash every glyph out along with the background; and the bar's own
# position must clamp so it can never leave the screen once the selection is
# dragged down to the bottom edge.

# Reverse of tokens.SHORTCUTS ("P" -> "pen"), so a button can look up its own
# key by tool name instead of every button re-scanning the forward mapping.
_TOOL_SHORTCUT_KEYS = {tool: key for key, tool in design.tokens.SHORTCUTS.items()}


def _tool_label(tool: str) -> str:
    """Human-facing text for a `tokens.TOOLS` entry.

    Mirrors editor.py's `_tool_label` (SNX-26) -- same title-casing -- over
    the redesign's plain string tool identifiers rather than the old `Tool`
    enum.
    """
    return tool.replace("_", " ").title()


class _IconButton(QPushButton):
    """One 34px icon button in the floating bar: a tool, undo, redo, clear
    or copy. A real `QPushButton`, not a rectangle painted by some
    ancestor's paintEvent, so its tooltip and click handling come for free.

    Idle/hover/active/disabled/danger-hover each recolour the glyph itself,
    not just the background -- per the spec's button-state table, "active
    tool... with an #f8faf0 glyph" -- so a state change regenerates the icon
    pixmap via `design.icon()` rather than leaning on a stylesheet, which has
    no way to reach into an SVG's `currentColor` stroke.
    """

    def __init__(
        self,
        icon_name: str,
        tooltip: str,
        *,
        idle_color: QColor | None = None,
        hover_bg: QColor | None = None,
        hover_color: QColor | None = None,
        parent=None,
    ):
        super().__init__(parent)
        metric = design.tokens.Metric
        self._icon_name = icon_name
        self._idle_color = idle_color or design.color("ICON_IDLE")
        self._hover_bg = hover_bg or design.color("ICON_HOVER_BG")
        # None (every button but Clear) means hovering leaves the glyph's
        # own colour alone -- only Clear's danger hover recolours the icon
        # as well as the background.
        self._hover_color = hover_color
        self._active = False

        self.setFixedSize(metric.BTN, metric.BTN)
        self.setIconSize(QSize(metric.ICON, metric.ICON))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(tooltip)
        self.setFlat(True)
        self._refresh()

    @property
    def is_active(self) -> bool:
        return self._active

    def set_active(self, active: bool) -> None:
        self._active = active
        self._refresh()

    def setEnabled(self, enabled: bool) -> None:
        super().setEnabled(enabled)
        self._refresh()

    def enterEvent(self, event) -> None:
        self._refresh(hovered=True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._refresh(hovered=False)
        super().leaveEvent(event)

    def _refresh(self, hovered: bool | None = None) -> None:
        if hovered is None:
            hovered = self.underMouse()
        metric = design.tokens.Metric

        if not self.isEnabled():
            # Per the README's "Undo / redo": disabled is the preferred way
            # to show an empty stack, over just recolouring a still-live
            # button -- so this is a real QWidget.setEnabled(False), not a
            # cosmetic-only state.
            bg, glyph = None, design.color("ICON_DISABLED")
        elif hovered and self._hover_color is not None:
            bg, glyph = self._hover_bg, self._hover_color
        elif self._active:
            bg, glyph = design.color("ICON_ACTIVE_BG"), design.color("ICON_ACTIVE")
        elif hovered:
            bg, glyph = self._hover_bg, self._idle_color
        else:
            bg, glyph = None, self._idle_color

        if bg is not None:
            self.setStyleSheet(
                "QPushButton { border: none; border-radius: %dpx;"
                " background: rgba(%d, %d, %d, %s); }"
                % (metric.BTN_RADIUS, bg.red(), bg.green(), bg.blue(), bg.alphaF())
            )
        else:
            self.setStyleSheet("QPushButton { border: none; background: transparent; }")

        # design.icon() only ever fills QIcon.Mode.Normal; left at that,
        # Qt's style would auto-generate its own faded Disabled variant the
        # moment setEnabled(False) runs above, undoing the exact
        # ICON_DISABLED colour just chosen. Registering the same pixmap for
        # both modes makes the disabled state use precisely what was asked
        # for instead of a second, uncontrolled recolouring on top of it.
        pixmap = design.icon(self._icon_name, glyph).pixmap(metric.ICON, metric.ICON)
        icon = QIcon()
        icon.addPixmap(pixmap, QIcon.Mode.Normal)
        icon.addPixmap(pixmap, QIcon.Mode.Disabled)
        self.setIcon(icon)


class _PillButton(QPushButton):
    """The two bar controls that pair an icon with a text label inside a
    solid pill: the capture-mode chip (row 1) and Save (row 17).

    Built from a child layout of two `QLabel`s rather than
    `QPushButton.setIcon`/`setText`, which always places the icon first --
    the chip needs its chevron *after* the label, per the spec's "label +
    14px chevron".
    """

    def __init__(
        self,
        icon_name: str,
        text: str,
        *,
        icon_size: int,
        text_color: QColor,
        bg_color: QColor,
        icon_after: bool,
        pad_left: int,
        pad_right: int,
        tooltip: str,
        parent=None,
    ):
        super().__init__(parent)
        metric = design.tokens.Metric
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(tooltip)
        self.setFixedHeight(metric.CHIP_H)
        self.setStyleSheet(
            "QPushButton { border: none; border-radius: %dpx;"
            " background: rgba(%d, %d, %d, %s); }"
            % (
                metric.BTN_RADIUS,
                bg_color.red(),
                bg_color.green(),
                bg_color.blue(),
                bg_color.alphaF(),
            )
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(pad_left, 0, pad_right, 0)
        layout.setSpacing(6)

        icon_label = QLabel(self)
        icon_label.setPixmap(design.icon(icon_name, text_color).pixmap(icon_size, icon_size))
        # A click must reach the QPushButton underneath, not stop at a
        # child QLabel sitting on top of it.
        icon_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        text_label = QLabel(text, self)
        text_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        font = QFont(design.font_families().ui)
        size, weight = design.tokens.Font.CHIP_LABEL
        font.setPixelSize(round(size))
        font.setWeight(QFont.Weight(weight))
        text_label.setFont(font)
        text_label.setStyleSheet(f"color: {text_color.name()};")

        for widget in (text_label, icon_label) if icon_after else (icon_label, text_label):
            layout.addWidget(widget)


class _Divider(QWidget):
    """1px vertical divider between the bar's button groups."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(1, design.tokens.Metric.DIVIDER_H)
        colour = design.color("DIVIDER")
        self.setStyleSheet(
            "background: rgba(%d, %d, %d, %s);"
            % (colour.red(), colour.green(), colour.blue(), colour.alphaF())
        )


class FloatingBar(QWidget):
    """The overlay redesign's floating bar: capture chip, eight tool
    buttons, undo/redo/clear, copy and save, per
    docs/design/overlay-redesign.md's "Floating bar" section -- eleven
    groups in place of the twenty-plus controls the old editor.py toolbar
    had, and per the ticket that reduction is not meant to grow back.

    A real child widget of `OverlayWindow`, built from real `QPushButton`s
    -- never painted inside `OverlayWindow.paintEvent` -- which is what
    gives every control a tooltip and hover state for free instead of
    hand-rolled hit-testing. `paintEvent` below paints the glass fill as a
    translucent *brush*, not a reduced-*opacity* widget: `setWindowOpacity`
    would dim every child glyph along with the background, exactly the
    mistake the README calls out.
    """

    toolSelected = pyqtSignal(str)
    undoRequested = pyqtSignal()
    redoRequested = pyqtSignal()
    clearRequested = pyqtSignal()
    copyRequested = pyqtSignal()
    saveRequested = pyqtSignal()
    captureChipClicked = pyqtSignal()

    UNDO_SHORTCUT = "Ctrl+Z"
    REDO_SHORTCUT = "Ctrl+Shift+Z"

    # The README gives the top clamp as this literal pixel value, not a
    # tokens.Metric entry -- same convention OverlayWindow's own
    # _TOP_CLEARANCE/_BAR_ROOM already follow for prose-only constants.
    _TOP_MAX_FROM_BOTTOM = 118

    def __init__(self, parent=None):
        super().__init__(parent)
        # The widget's own backdrop is transparent so paintEvent's alpha
        # fill is the only thing establishing a background colour --
        # without this attribute Qt composites the widget as opaque and the
        # "93% alpha, not 93% opacity" distinction has nothing to paint
        # against.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        metric = design.tokens.Metric
        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            metric.BAR_PAD_H, metric.BAR_PAD_V, metric.BAR_PAD_H, metric.BAR_PAD_V
        )
        layout.setSpacing(metric.BAR_GAP)

        self._active_tool: str | None = None
        self._tool_buttons: dict[str, _IconButton] = {}

        self._chip = self._build_capture_chip()
        self._chip.clicked.connect(self.captureChipClicked)
        layout.addWidget(self._chip)
        self._add_divider(layout)

        for tool in design.tokens.TOOLS:
            key = _TOOL_SHORTCUT_KEYS[tool]
            button = _IconButton(tool, f"{_tool_label(tool)} — {key}")
            button.clicked.connect(lambda checked=False, t=tool: self._on_tool_clicked(t))
            self._tool_buttons[tool] = button
            layout.addWidget(button)
        self._add_divider(layout)

        self._undo_button = _IconButton("undo", f"Undo — {self.UNDO_SHORTCUT}")
        self._undo_button.clicked.connect(self.undoRequested)
        self._undo_button.setEnabled(False)
        layout.addWidget(self._undo_button)

        self._redo_button = _IconButton("redo", f"Redo — {self.REDO_SHORTCUT}")
        self._redo_button.clicked.connect(self.redoRequested)
        self._redo_button.setEnabled(False)
        layout.addWidget(self._redo_button)

        self._clear_button = _IconButton(
            "trash",
            "Clear ink",
            hover_bg=design.color("DANGER_BG"),
            hover_color=design.color("DANGER_FG"),
        )
        self._clear_button.clicked.connect(self.clearRequested)
        layout.addWidget(self._clear_button)
        self._add_divider(layout)

        self._copy_button = _IconButton("copy", "Copy", idle_color=design.color("ICON_NEUTRAL"))
        self._copy_button.clicked.connect(self.copyRequested)
        layout.addWidget(self._copy_button)

        self._save_button = self._build_save_button()
        self._save_button.clicked.connect(self.saveRequested)
        layout.addWidget(self._save_button)

    # -- construction helpers ------------------------------------------------

    def _add_divider(self, layout: QHBoxLayout) -> None:
        metric = design.tokens.Metric
        layout.addSpacing(metric.BAR_DIVIDER_GAP)
        layout.addWidget(_Divider(self))
        layout.addSpacing(metric.BAR_DIVIDER_GAP)

    def _build_capture_chip(self) -> _PillButton:
        label, _icon, _note = design.tokens.CAPTURE_MODES[0]  # "Region", the bar's default
        metric = design.tokens.Metric
        return _PillButton(
            "chevron",
            label,
            icon_size=14,
            text_color=design.color("ACCENT_FG"),
            bg_color=design.color("ACCENT"),
            icon_after=True,
            pad_left=metric.CHIP_PAD_L,
            pad_right=metric.CHIP_PAD_R,
            tooltip="Capture mode",
        )

    def _build_save_button(self) -> _PillButton:
        metric = design.tokens.Metric
        return _PillButton(
            "save",
            "Save",
            icon_size=metric.ICON,
            text_color=design.color("TEXT_PRIMARY"),
            # No token names this fill on its own; it is the same
            # #ffffff/10% pair tokens.Color.BAR_BORDER already defines, so
            # that's reused here rather than re-typed, per CLAUDE.md's
            # "import from tokens rather than re-typing literals."
            bg_color=design.color("BAR_BORDER"),
            icon_after=False,
            pad_left=13,
            pad_right=13,
            tooltip="Save",
        )

    # -- tool selection --------------------------------------------------

    def _on_tool_clicked(self, tool: str) -> None:
        self.set_active_tool(tool)
        self.toolSelected.emit(tool)

    def set_active_tool(self, tool: str | None) -> None:
        """Mark `tool`'s button active and every other tool button idle.

        The single place this is enforced, whether the change came from a
        click above or a caller driving the bar directly -- per the spec,
        exactly one tool reads as active at a time.
        """
        self._active_tool = tool
        for name, button in self._tool_buttons.items():
            button.set_active(name == tool)

    @property
    def active_tool(self) -> str | None:
        return self._active_tool

    # -- undo / redo -------------------------------------------------------

    def set_undo_enabled(self, enabled: bool) -> None:
        self._undo_button.setEnabled(enabled)

    def set_redo_enabled(self, enabled: bool) -> None:
        self._redo_button.setEnabled(enabled)

    # -- fill ----------------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        metric = design.tokens.Metric
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        # design.color("BAR_BG") already carries BAR_BG_ALPHA (93%) --
        # painted here as a translucent *fill*, never as reduced *widget*
        # opacity, so every child painted after this (each button's own
        # glyph) stays fully opaque.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(design.color("BAR_BG"))
        painter.drawRoundedRect(rect, metric.BAR_RADIUS, metric.BAR_RADIUS)

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(design.color("BAR_BORDER"))
        painter.drawRoundedRect(rect, metric.BAR_RADIUS, metric.BAR_RADIUS)
        painter.end()

    # -- positioning -----------------------------------------------------

    def reposition(self, selection: QRect, bounds_size: QSize) -> None:
        """Centre the bar under `selection`, clamped so it can never leave a
        window of `bounds_size` -- even with the selection dragged to the
        very bottom edge -- per the spec's "Floating bar" clamp rule.
        """
        metric = design.tokens.Metric
        size = self.sizeHint()
        # QRectF, not the raw QRect `selection`: QRect.bottom() is
        # inclusive (top + height - 1), which would put the bar a pixel
        # higher than intended -- same fix OverlayWindow's own
        # `_bracket_path` already applies for the same reason.
        sel = QRectF(selection)

        desired_center_x = sel.center().x()
        # Falls back to the window's own centre, rather than inverting, when
        # the window is narrower than twice BAR_MIN_EDGE -- a case the
        # README's "at least 400px from either screen edge" doesn't
        # anticipate (a real screen is always wider than 800px) but a small
        # test/embedded window can hit.
        half_width = bounds_size.width() / 2
        min_center = min(metric.BAR_MIN_EDGE, half_width)
        max_center = max(bounds_size.width() - metric.BAR_MIN_EDGE, half_width)
        center_x = max(min_center, min(desired_center_x, max_center))

        desired_top = sel.bottom() + metric.BAR_OFFSET_Y
        top = min(desired_top, bounds_size.height() - self._TOP_MAX_FROM_BOTTOM)

        self.setGeometry(
            round(center_x - size.width() / 2), round(top), size.width(), size.height()
        )


# ---------------------------------------------------------------------------
# Settings tray (SNX-41)
# ---------------------------------------------------------------------------
# docs/design/overlay-redesign.md's "Settings tray" section is the authority
# here. The conditional visibility is the design's core idea, not a detail:
# colour and stroke are not controls until the user is holding something
# that draws, which is what keeps the bar itself at eleven groups instead of
# growing a twelfth for settings that only sometimes apply. tokens.DRAW_TOOLS
# names the tools that get *this* tray; blur gets a different one (a later
# ticket) and the eraser gets none at all.


class _ToolPill(QWidget):
    """The tray's leftmost control: a static (non-clickable) pill naming
    the active tool -- glyph + label, per the spec's "Active-tool pill"
    bullet. A plain QWidget, not a QPushButton: nothing here is clickable,
    only its translucent fill and its two child QLabels change when the
    tool does.
    """

    # Not a tokens.Color entry -- no other control in the design uses this
    # exact 8% white fill, so it lives here as a class constant the same
    # way FloatingBar's own _TOP_MAX_FROM_BOTTOM keeps a README literal
    # that isn't a token.
    _BG_ALPHA = 0.08
    _RADIUS = 8
    _ICON_SIZE = 14

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        # Padding "3/9/3/6" per the spec -- CSS top/right/bottom/left --
        # asymmetric because the glyph sits close to the pill's own rounded
        # left edge while the label needs more room on the right.
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 3, 9, 3)
        layout.setSpacing(6)

        self._icon_label = QLabel(self)
        layout.addWidget(self._icon_label)

        self._text_label = QLabel(self)
        font = QFont(design.font_families().ui)
        size, weight = design.tokens.Font.TRAY_LABEL
        font.setPixelSize(round(size))
        font.setWeight(QFont.Weight(weight))
        self._text_label.setFont(font)
        self._text_label.setStyleSheet(f"color: {design.color('TEXT_PRIMARY').name()};")
        layout.addWidget(self._text_label)

    def set_tool(self, tool: str) -> None:
        pixmap = design.icon(tool, design.color("TEXT_PRIMARY")).pixmap(
            self._ICON_SIZE, self._ICON_SIZE
        )
        self._icon_label.setPixmap(pixmap)
        self._text_label.setText(_tool_label(tool))

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg = QColor("#ffffff")
        bg.setAlphaF(self._BG_ALPHA)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg)
        painter.drawRoundedRect(QRectF(self.rect()), self._RADIUS, self._RADIUS)
        painter.end()


class _SwatchButton(QPushButton):
    """One 22px ink swatch in the tray. A real `QPushButton` for its click
    handling and tooltip, with the fill/border/ring hand-painted -- Qt's
    stylesheet has no primitive for the spec's two-colour selection ring,
    so `paintEvent` is overridden the same way `FloatingBar.paintEvent`
    already hand-paints a translucent fill rather than leaning on QSS.
    """

    # Prose-only literals from the spec's "Seven ink swatches" bullet, not
    # tokens.Metric entries -- same convention as OverlayWindow's own
    # _CORNER_BRACKET_OFFSET and friends.
    _BORDER_ALPHA = 0.20
    _RING_W = 1.5      # the light outer ring
    _RING_GAP_W = 2.0  # the dark gap between the ring and the fill

    def __init__(self, name: str, hex_colour: str, parent=None):
        super().__init__(parent)
        metric = design.tokens.Metric
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._colour = QColor(hex_colour)
        self._selected = False
        self.setFixedSize(metric.SWATCH, metric.SWATCH)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(name)
        self.setFlat(True)
        self.setStyleSheet("QPushButton { border: none; background: transparent; }")

    @property
    def hex_colour(self) -> str:
        return self._colour.name()

    @property
    def is_selected(self) -> bool:
        return self._selected

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        metric = design.tokens.Metric
        rect = QRectF(self.rect())
        radius = metric.SWATCH_RADIUS

        if self._selected:
            # The spec's double ring is an outward box-shadow --
            # "0 0 0 2px #1a1c18, 0 0 0 3.5px #f1f3e8" -- reproduced inward
            # here since a fixed-size button has no room to paint past its
            # own bounds: light ring at the very edge, dark gap inside it,
            # fill inside that -- the same near-to-far order the box-shadow
            # gives reading outward from the fill.
            painter.setBrush(design.color("TEXT_PRIMARY"))
            painter.drawRoundedRect(rect, radius, radius)
            gap_rect = rect.adjusted(
                self._RING_W, self._RING_W, -self._RING_W, -self._RING_W
            )
            painter.setBrush(design.color("BAR_BG"))
            painter.drawRoundedRect(
                gap_rect, max(radius - self._RING_W, 0), max(radius - self._RING_W, 0)
            )
            inset = self._RING_W + self._RING_GAP_W
        else:
            border = QColor("#ffffff")
            border.setAlphaF(self._BORDER_ALPHA)
            painter.setPen(QPen(border, 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            border_rect = rect.adjusted(0.5, 0.5, -0.5, -0.5)
            painter.drawRoundedRect(border_rect, radius, radius)
            painter.setPen(Qt.PenStyle.NoPen)
            inset = 1.0

        fill_rect = rect.adjusted(inset, inset, -inset, -inset)
        painter.setBrush(self._colour)
        painter.drawRoundedRect(fill_rect, max(radius - inset, 0), max(radius - inset, 0))
        painter.end()


class _CustomColorButton(QPushButton):
    """The tray's "custom colour" control: the same 22px box as a swatch,
    but a dashed, dim border and a plus glyph instead of a colour fill --
    per the spec's "custom colour" bullet. `SettingsTray` opens
    `QColorDialog` on its click and wires the result back in, mirroring
    editor.py's own `_pick_colour`.
    """

    _BORDER_ALPHA = 0.32

    def __init__(self, parent=None):
        super().__init__(parent)
        metric = design.tokens.Metric
        self.setFixedSize(metric.SWATCH, metric.SWATCH)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Custom colour")
        self.setFlat(True)
        border = QColor("#ffffff")
        border.setAlphaF(self._BORDER_ALPHA)
        # A dashed border and a transparent fill are both plain QSS here --
        # unlike _SwatchButton's ring, there's only ever one border to draw,
        # so a stylesheet is enough and a custom paintEvent isn't needed.
        self.setStyleSheet(
            "QPushButton { border: 1px dashed rgba(%d, %d, %d, %s);"
            " border-radius: %dpx; background: transparent; }"
            % (
                border.red(),
                border.green(),
                border.blue(),
                border.alphaF(),
                metric.SWATCH_RADIUS,
            )
        )
        self.setIcon(design.icon("plus", design.color("TEXT_PRIMARY")))
        self.setIconSize(QSize(12, 12))


class _PreviewDot(QWidget):
    """The tray's live preview: a filled circle of the current ink colour
    at the current stroke's diameter, inside a fixed 28px box, per the
    spec's "Live preview dot" bullet. `set_preview` is the single entry
    point `SettingsTray` calls whenever colour, stroke or tool changes, so
    this widget itself holds no state that could fall out of sync with it.
    """

    _BOX = 28

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedSize(self._BOX, self._BOX)
        self._colour = QColor(design.tokens.INK_SWATCHES[0][1])
        self._diameter = float(design.tokens.Metric.STROKE_DEFAULT)

    def set_preview(self, colour: QColor, diameter: float) -> None:
        self._colour = colour
        self._diameter = diameter
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._colour)
        center = QRectF(self.rect()).center()
        radius = self._diameter / 2
        painter.drawEllipse(center, radius, radius)
        painter.end()


class SettingsTray(QWidget):
    """The overlay redesign's settings tray: an active-tool pill, the ink
    swatches, a custom-colour button, a stroke slider/readout and a live
    preview dot, per docs/design/overlay-redesign.md's "Settings tray"
    section.

    Visible only once `set_tool` is called with a member of
    `tokens.DRAW_TOOLS` -- every other tool (blur, whose own strength/mode
    tray is a later ticket, and the eraser, which gets no tray at all)
    hides this one outright, per the spec: "colour and stroke are not
    controls until the user is holding something that draws."

    A real child widget, built the same way `FloatingBar` is -- never
    painted inside `OverlayWindow.paintEvent` -- so its buttons, slider and
    tooltips come for free.
    """

    colourChanged = pyqtSignal(str)
    strokeChanged = pyqtSignal(int)

    # The README gives this literal directly ("minimum width 34px") rather
    # than as a tokens.Metric entry -- same convention FloatingBar's own
    # _TOP_MAX_FROM_BOTTOM already follows for a prose-only constant. This
    # is what keeps the tray from reflowing as the stroke readout's digit
    # count changes width.
    _READOUT_MIN_W = 34

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        metric = design.tokens.Metric
        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            metric.TRAY_PAD_H, metric.TRAY_PAD_V, metric.TRAY_PAD_H, metric.TRAY_PAD_V
        )
        layout.setSpacing(metric.TRAY_GAP)

        self._tool: str = design.tokens.DRAW_TOOLS[0]
        self._colour: str = design.tokens.INK_SWATCHES[0][1]
        self._stroke: int = metric.STROKE_DEFAULT

        self._pill = _ToolPill(self)
        layout.addWidget(self._pill)
        layout.addWidget(_Divider(self))

        self._swatch_buttons: dict[str, _SwatchButton] = {}
        for name, hex_colour in design.tokens.INK_SWATCHES:
            button = _SwatchButton(name, hex_colour, self)
            button.clicked.connect(lambda checked=False, c=hex_colour: self.set_colour(c))
            self._swatch_buttons[hex_colour] = button
            layout.addWidget(button)

        self._custom_button = _CustomColorButton(self)
        self._custom_button.clicked.connect(self._on_custom_colour_clicked)
        layout.addWidget(self._custom_button)
        layout.addWidget(_Divider(self))

        self._slider = QSlider(Qt.Orientation.Horizontal, self)
        self._slider.setRange(metric.STROKE_MIN, metric.STROKE_MAX)
        self._slider.setValue(self._stroke)
        self._slider.setFixedWidth(metric.SLIDER_W)
        self._slider.valueChanged.connect(self.set_stroke)
        layout.addWidget(self._slider)

        self._readout = QLabel(self)
        self._readout.setMinimumWidth(self._READOUT_MIN_W)
        font = QFont(design.font_families().mono)
        size, weight = design.tokens.Font.READOUT
        font.setPixelSize(round(size))
        font.setWeight(QFont.Weight(weight))
        self._readout.setFont(font)
        self._readout.setStyleSheet(f"color: {design.color('TEXT_READOUT').name()};")
        layout.addWidget(self._readout)

        self._preview = _PreviewDot(self)
        layout.addWidget(self._preview)
        layout.addWidget(_Divider(self))

        self._hint = QLabel(self)
        hint_font = QFont(design.font_families().ui)
        size, weight = design.tokens.Font.TRAY_HINT
        hint_font.setPixelSize(round(size))
        hint_font.setWeight(QFont.Weight(weight))
        self._hint.setFont(hint_font)
        self._hint.setStyleSheet(f"color: {design.color('TEXT_MUTED').name()};")
        layout.addWidget(self._hint)

        self._select_swatch(self._colour)
        self._pill.set_tool(self._tool)
        self._hint.setText(design.tokens.TOOL_HINTS.get(self._tool, ""))
        self._refresh_readout_and_preview()

    # -- state ---------------------------------------------------------

    @property
    def tool(self) -> str:
        return self._tool

    @property
    def colour(self) -> str:
        return self._colour

    @property
    def stroke(self) -> int:
        return self._stroke

    def set_tool(self, tool: str) -> None:
        """Show this tray for `tool` if it's one of `tokens.DRAW_TOOLS`,
        hide it otherwise -- the tray's whole reason for existing, per the
        spec's "Settings tray" section.
        """
        self._tool = tool
        if tool in design.tokens.DRAW_TOOLS:
            self._pill.set_tool(tool)
            self._hint.setText(design.tokens.TOOL_HINTS.get(tool, ""))
            self._refresh_readout_and_preview()
            self.show()
        else:
            self.hide()

    def set_colour(self, hex_colour: str) -> None:
        """Set the current ink colour -- from a swatch click or the custom
        colour dialog -- and repaint the selection ring and preview dot to
        match. This is the colour new marks are drawn in.
        """
        self._colour = hex_colour
        self._select_swatch(hex_colour)
        self._refresh_readout_and_preview()
        self.colourChanged.emit(hex_colour)

    def set_stroke(self, stroke: int) -> None:
        """Set the current stroke width, clamped to `tokens.Metric`'s
        `STROKE_MIN`/`STROKE_MAX` range, and refresh the readout/preview.
        """
        metric = design.tokens.Metric
        stroke = max(metric.STROKE_MIN, min(stroke, metric.STROKE_MAX))
        self._stroke = stroke
        if self._slider.value() != stroke:
            self._slider.setValue(stroke)
        self._refresh_readout_and_preview()
        self.strokeChanged.emit(stroke)

    def _select_swatch(self, hex_colour: str) -> None:
        for colour, button in self._swatch_buttons.items():
            button.set_selected(colour.lower() == hex_colour.lower())

    def _on_custom_colour_clicked(self) -> None:
        # QColorDialog.getColor() returns an invalid QColor on Cancel
        # (rather than raising or returning None), so isValid() is the
        # correct "did the user actually choose something" check here --
        # same as editor.py's own _pick_colour.
        colour = QColorDialog.getColor(QColor(self._colour), self, "Custom Colour")
        if colour.isValid():
            self.set_colour(colour.name())

    def _refresh_readout_and_preview(self) -> None:
        self._readout.setText(f"{self._stroke}px")
        metric = design.tokens.Metric
        # The highlighter's stroke paints wider than every other tool
        # (HIGHLIGHT_MULT, see shapes.py) -- the preview dot mirrors that
        # so it shows the mark's real drawn size, not just the slider's raw
        # number. Clamped to the same STROKE_MIN/STROKE_MAX range the
        # slider itself uses, per the ticket, rather than the 28px box's
        # own literal size.
        mult = metric.HIGHLIGHT_MULT if self._tool == "highlighter" else 1.0
        diameter = max(metric.STROKE_MIN, min(self._stroke * mult, metric.STROKE_MAX))
        self._preview.set_preview(QColor(self._colour), diameter)


class OverlayWindow(QWidget):
    """The overlay redesign's shell: one frameless window spanning the whole
    virtual desktop, per docs/design/overlay-redesign.md.

    SNX-31 built the background frame and dim scrim; SNX-32 added the
    selection frame -- the marching-ants stroke, corner brackets and edge
    handles; SNX-33 made that selection re-frameable. SNX-34 (this ticket)
    adds the ink layer itself: `_marks` are stored and painted in this
    widget's own window coordinates, clipped to the selection, per
    docs/design/overlay-redesign.md's "Ink lives in screen coordinates" --
    so re-framing moves the clip over marks that never move themselves.
    SNX-38 adds the eraser: `erase_at`/`undo_erase`, hit-testing marks via
    `Shape.hit_test` only while `set_eraser_active` has armed it, per the
    spec's "Marks become hit-testable only while the eraser is active."
    SNX-39 adds the general undo/redo/clear stack over `_marks` itself
    (`undo`/`redo`/`clear`, distinct from the eraser's own single-slot
    `undo_erase`) plus `copy`/`save`, which render `_marks` fresh at the
    moment they're called -- replacing the old editor.py flow's bug of
    copying the un-annotated capture once, before any annotation could
    exist. SNX-40 adds `FloatingBar` itself as a real child widget (`_bar`),
    wired to `undo`/`redo`/`clear`/`copy`/`save` and to `set_eraser_active`,
    and kept positioned under `_selection` by `_sync_bar_visibility`.
    SNX-41 (this ticket) adds `SettingsTray` (`_tray`), shown and positioned
    under the bar by `_sync_tray_visibility` only while the bar's active
    tool is one of `tokens.DRAW_TOOLS` -- the eraser (and, for now, blur,
    whose own tray is a later ticket) gets none -- and tracks the colour/
    stroke it emits as `_ink_colour`/`_stroke_width` for a later ticket's
    drawing tools to read. The drawing-tool mouse handling that would
    actually call `add_mark` from a live drag -- for every tool but the
    eraser, which SNX-38 already wired end to end -- is still a later
    ticket in the same arc.

    Unlike `Overlay` above -- one instance per monitor, selection kept in
    absolute logical virtual-desktop coordinates so per-monitor crops tile
    correctly -- this is a *single* window covering the whole desktop, and
    per the spec's state table (`sel: QRect # window coords`) its selection
    is kept in window coordinates: local to this widget's own top-left, not
    the virtual desktop's. `frame` is expected to be a single capture
    already spanning every monitor -- what `BackendRegistry.capture()`
    returns -- not a per-monitor crop.

    Per CLAUDE.md's one architectural rule, the compositor is asked for
    pixels exactly once, upstream in `capture.py`; the frame handed in here
    is already frozen, and the spec's own deviation note applies: this never
    uses `QScreen.grabWindow(0)`, which returns black on Wayland.
    """

    # Marching ants: a QTimer at ~30fps advancing the dashed pen's offset,
    # per the README's Qt note -- not tied to paintEvent's own cadence, so
    # the animation speed doesn't drift with however often something else
    # triggers a repaint.
    _ANTS_TIMER_INTERVAL_MS = 33

    # Straddle offsets and corner radii the design specifies by pixel value
    # in the README's prose (and the HTML reference) rather than as a named
    # `tokens.Metric` constant -- unlike arm length/thickness and handle
    # dimensions, which *are* tokenized and read from there below.
    _CORNER_BRACKET_OFFSET = 2
    _CORNER_BRACKET_RADIUS = 3
    _EDGE_HANDLE_OFFSET = 5
    _EDGE_HANDLE_RADIUS = 5
    _CORNER_HIT_OFFSET = 7

    # Re-framing clamps (SNX-33), likewise given by the README's "Re-framing"
    # prose as plain pixel values rather than a tokens.Metric entry -- the
    # minimum size itself *is* tokenized (SEL_MIN_W/H below), since that one
    # the ticket explicitly overrides from the spec's default.
    _TOP_CLEARANCE = 52  # keeps the selection clear of the top hint HUD
    _BAR_ROOM = 130  # keeps room below the selection for the floating bar

    def __init__(self, frame: Frame, parent=None):
        super().__init__(parent)
        self._frame = frame

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        # The frame's own logical origin/size *is* the virtual desktop's
        # bounds -- a full capture already spans every monitor, so no union
        # of screen geometries needs computing here the way create_overlays()
        # does for the per-monitor Overlay above.
        self.setGeometry(
            round(frame.logical_origin.x()),
            round(frame.logical_origin.y()),
            round(frame.logical_size.width()),
            round(frame.logical_size.height()),
        )
        # Needed for the handle cursors below to update on a plain hover,
        # not just while a button is held -- mirrors `Overlay.setMouseTracking`
        # above for the same reason.
        self.setMouseTracking(True)

        # Window coordinates, per the class docstring -- None until
        # set_selection is called.
        self._selection: QRect | None = None

        # Handle currently being dragged (SNX-33 re-framing), and the
        # selection as it stood the moment that drag started. The anchor is
        # read-only for the drag's whole duration -- every edge it doesn't
        # own comes from here, never from the live selection -- which is
        # what keeps the opposite edge/corner from creeping as the mouse
        # moves. None outside a handle drag.
        self._active_handle: Handle | None = None
        self._resize_anchor: QRect | None = None

        # The ink layer (SNX-34): overlay-window coordinates, the same
        # space `_selection` lives in above -- never translated relative to
        # the selection, per the class docstring. Paint order is the list
        # order, mirroring shapes.py's render().
        self._marks: list[Shape] = []
        # Redo stack (SNX-39): whole marks popped off the *end* of `_marks`
        # by undo(), in the order undo() popped them -- so redo() popping
        # this list's own end and appending back to `_marks` restores each
        # one to exactly the draw-order position it was undone from. Never
        # touched by erase_at/undo_erase, which is its own single-slot undo
        # scoped to the eraser tool alone -- see that pair's docstrings.
        self._redo: list[Shape] = []

        # Eraser tool state (SNX-38). False until a caller (the floating
        # bar's eraser button, a later ticket) arms it via
        # set_eraser_active -- marks are only ever hit-tested while this is
        # True, per docs/design/overlay-redesign.md's "Drawing": "hit-
        # testable only while the eraser is active," so ordinary drawing
        # never pays for it.
        self._eraser_active = False
        # (index, shape) most recently removed by erase_at, restorable via
        # undo_erase() -- see its own docstring. None once nothing has been
        # erased yet, or once undo_erase() has already consumed it.
        self._erased_mark: tuple[int, Shape] | None = None

        self._dash_offset = 0.0
        self._ants_timer = QTimer(self)
        self._ants_timer.setInterval(self._ANTS_TIMER_INTERVAL_MS)
        self._ants_timer.timeout.connect(self._advance_ants)

        # The floating bar (SNX-40): a real child widget, positioned under
        # the selection by `_sync_bar` below rather than drawn in this
        # class's own paintEvent. Starts hidden -- shown only once this
        # window itself is shown (see showEvent/hideEvent) so a caller that
        # never shows the window (most of this file's own tests, which grab()
        # an unshown widget to sample pixels) never has the bar painted over
        # whatever they're sampling.
        self._bar = FloatingBar(self)
        self._bar.hide()
        self._bar.undoRequested.connect(self.undo)
        self._bar.redoRequested.connect(self.redo)
        self._bar.clearRequested.connect(self.clear)
        self._bar.copyRequested.connect(self.copy)
        self._bar.saveRequested.connect(self.save)
        self._bar.toolSelected.connect(self._on_tool_selected)

        # The settings tray (SNX-41): shown only while the bar's active
        # tool is one of tokens.DRAW_TOOLS -- see `_sync_tray_visibility`,
        # which `_sync_bar_visibility` calls alongside the bar's own
        # show/hide so the two stay in lockstep with `_selection` and this
        # window's own visibility, for the same reason `_bar` is gated
        # there rather than following `_selection` unconditionally.
        self._ink_colour: str = design.tokens.INK_SWATCHES[0][1]
        self._stroke_width: int = design.tokens.Metric.STROKE_DEFAULT
        self._tray = SettingsTray(self)
        self._tray.hide()
        self._tray.colourChanged.connect(self._on_ink_colour_changed)
        self._tray.strokeChanged.connect(self._on_stroke_width_changed)

    def set_selection(self, rect: QRect | None) -> None:
        """Set the current selection (window coordinates) and repaint."""
        self._selection = rect
        self._sync_bar_visibility()
        self.update()

    def _on_tool_selected(self, tool: str) -> None:
        """Wire the bar's tool buttons to the one piece of per-tool state
        this class already tracks: the eraser's hit-testing arm/disarm (see
        `set_eraser_active`). Switching the *live drawing* tool itself --
        pen, arrow, and the rest actually starting a stroke on drag -- is
        still a later ticket in the same arc, per the class docstring; this
        only has to keep the eraser cursor and hit-testing in sync with
        whichever tool button the bar shows as active.
        """
        self.set_eraser_active(tool == "eraser")
        self._sync_tray_visibility()

    def _on_ink_colour_changed(self, hex_colour: str) -> None:
        """Track the tray's current ink colour -- "the colour new marks are
        drawn in" -- for the still-later ticket that wires the drawing
        tools themselves up to read it when a stroke starts.
        """
        self._ink_colour = hex_colour

    def _on_stroke_width_changed(self, stroke: int) -> None:
        self._stroke_width = stroke

    def _sync_bar_visibility(self) -> None:
        """Show/hide and reposition the floating bar to match `_selection`.

        Guarded on `self.isVisible()` -- not just "is there a selection" --
        so this window's own many pixel-sampling tests, none of which ever
        call `.show()`, never end up with the bar painted into a `grab()`
        they didn't ask for: a child widget's own `.show()` call is enough
        to make Qt paint it in a `grab()` regardless of whether this window
        itself was ever shown, so visibility has to be gated here rather
        than unconditionally following `_selection`.
        """
        if self._selection is not None and self.isVisible():
            self._bar.reposition(self._selection, self.size())
            self._bar.show()
            self._sync_tray_visibility()
        else:
            self._bar.hide()
            self._tray.hide()

    def _sync_tray_visibility(self) -> None:
        """Show/hide and reposition the settings tray under the bar.

        Gated on the bar's own visibility rather than re-checking
        `_selection`/`self.isVisible()` directly -- the bar is already the
        single source of truth for "is this window's chrome allowed to be
        on screen right now," and the tray sits directly below it, so
        piggybacking on that check is what keeps the two from being able to
        disagree.
        """
        tool = self._bar.active_tool
        if self._bar.isVisible() and tool in design.tokens.DRAW_TOOLS:
            self._tray.set_tool(tool)
            self._reposition_tray()
        else:
            self._tray.hide()

    def _reposition_tray(self) -> None:
        """Centre the tray under the bar, `TRAY_OFFSET_Y` below it -- per
        the spec's "Sits 8px below the bar, centred on it."
        """
        metric = design.tokens.Metric
        bar_geometry = self._bar.geometry()
        size = self._tray.sizeHint()
        center_x = bar_geometry.center().x()
        top = bar_geometry.bottom() + metric.TRAY_OFFSET_Y
        self._tray.setGeometry(
            round(center_x - size.width() / 2), round(top), size.width(), size.height()
        )

    def _sync_bar_undo_redo(self) -> None:
        self._bar.set_undo_enabled(self.can_undo)
        self._bar.set_redo_enabled(self.can_redo)

    def add_mark(self, shape: Shape) -> None:
        """Append `shape` to the ink layer and repaint.

        `shape`'s points must already be in this widget's own window
        coordinates -- the same space mouse events and `_selection` use --
        never translated to be relative to the selection. That is what lets
        a re-frame leave every mark exactly where it was drawn: only the
        selection's clip rect moves, per the class docstring.

        Clears the redo stack (SNX-39): per docs/design/overlay-redesign.md's
        "Undo / redo", "any new mark clears the redo stack" -- a mark
        committed after an undo makes whatever was undone unreachable by
        redo again, same as any ordinary undo/redo history.
        """
        self._marks.append(shape)
        self._redo.clear()
        self._sync_bar_undo_redo()
        self.update()

    @property
    def marks(self) -> tuple[Shape, ...]:
        """Ink layer contents, in paint order. A copy, not the live list,
        mirroring `Canvas.shapes` in editor.py."""
        return tuple(self._marks)

    # -- undo / redo / clear (SNX-39) --------------------------------------
    # Two stacks of whole marks, per docs/design/overlay-redesign.md's
    # "Undo / redo": undo pops the newest mark off `_marks` onto `_redo`;
    # redo pops it back. Both are plain end-of-list push/pop, which is what
    # keeps a redone mark landing at exactly the draw-order position it was
    # undone from -- there is no index bookkeeping to get wrong.

    @property
    def can_undo(self) -> bool:
        return bool(self._marks)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> None:
        """Move the newest mark from the ink layer to the redo stack.

        A no-op with nothing to undo -- mirrors `Canvas.undo`'s guard in
        editor.py, just against `_marks` being empty rather than a history
        index, since this class keeps no separate history list.
        """
        if not self._marks:
            return
        self._redo.append(self._marks.pop())
        self._sync_bar_undo_redo()
        self.update()

    def redo(self) -> None:
        """Move the newest undone mark from the redo stack back onto the
        ink layer, at the same position in draw order it was undone from.

        A no-op with nothing to redo -- either nothing has been undone yet,
        or a mark committed since (see `add_mark`) already cleared the
        stack.
        """
        if not self._redo:
            return
        self._marks.append(self._redo.pop())
        self._sync_bar_undo_redo()
        self.update()

    def clear(self) -> None:
        """Discard every mark and both stacks in a single step.

        Per the spec: "Clear-ink empties both and toasts" -- and clearing
        is explicitly *not* itself undoable, unlike an ordinary undo/redo
        entry: the cleared marks are dropped outright rather than pushed
        onto `_redo`, so a subsequent undo() has nothing left to pop and
        cannot bring them back.
        """
        self._marks = []
        self._redo = []
        self._sync_bar_undo_redo()
        self.update()

    def set_eraser_active(self, active: bool) -> None:
        """Arm/disarm the eraser tool (SNX-38).

        While active, a plain left-click inside the selection that doesn't
        land on a resize handle removes the topmost mark under the cursor
        instead of being a no-op -- see mousePressEvent -- and the cursor
        over the selection switches from the drawing crosshair to a
        pointer, per docs/design/overlay-redesign.md's "Selection frame"
        cursor table ("crosshair for every tool except the eraser, which
        is pointer") -- see mouseMoveEvent.
        """
        self._eraser_active = active

    def erase_at(self, point: QPointF) -> Shape | None:
        """Remove and return the topmost mark under `point` (this widget's
        own window coordinates -- the same space `_marks` lives in), or
        None if nothing is there.

        Walks `_marks` back to front, mirroring `_paint_marks`'s (and
        render()'s) own draw-order-is-paint-order contract, so an overlap
        resolves to whichever mark is actually visible at that pixel --
        per docs/design/overlay-redesign.md's "Drawing": "a click deletes
        the topmost mark under the cursor." A miss removes nothing and
        raises nothing: not every click lands on ink, and that is not an
        error.

        The removed (index, shape) pair is stashed for undo_erase() to
        restore at the same list position it was removed from -- see that
        method's own docstring. A later erase_at call overwrites it, same
        as this tool has only ever removed one mark at a time.
        """
        for index in range(len(self._marks) - 1, -1, -1):
            if self._marks[index].hit_test(point):
                shape = self._marks.pop(index)
                self._erased_mark = (index, shape)
                self.update()
                return shape
        return None

    def undo_erase(self) -> None:
        """Restore the mark most recently removed by erase_at to its
        original position in draw order.

        A no-op if nothing has been erased since the last erase_at/
        undo_erase call -- this is a single slot of undo scoped to the
        eraser tool itself, not the general multi-action undo/redo stack
        docs/design/overlay-redesign.md's "Undo / redo" section describes,
        which is a later ticket's concern.
        """
        if self._erased_mark is None:
            return
        index, shape = self._erased_mark
        self._marks.insert(index, shape)
        self._erased_mark = None
        self.update()

    def rendered_image(self) -> QImage:
        """The final exported image: `_marks` flattened onto the current
        selection's crop of the frozen frame, translated from window
        coordinates to the cropped image's own origin exactly once -- see
        `shapes.render_selection` and docs/design/overlay-redesign.md's
        "Ink lives in screen coordinates".
        """
        if self._selection is None:
            raise ValueError("no selection to export")
        return render_selection(self._frame, self._marks, QRectF(self._selection))

    # -- copy / save (SNX-39) ----------------------------------------------
    # Both render fresh from `rendered_image()` at the moment they're
    # called, which is the actual fix this ticket makes: the old editor.py
    # flow (Editor.__init__) copied the raw, un-annotated capture to the
    # clipboard exactly once, before any annotation could exist, so the
    # clipboard never reflected marks made afterwards. Neither method
    # imports from app.py at module level -- app.py imports this module at
    # its own top level (to build overlays), so a top-level import back
    # would be circular; deferred here the same way
    # `AppController._on_confirmed` defers importing `Editor`.

    def copy(self) -> None:
        """Flatten the marks present *right now* onto the selection's crop
        and place the result on the clipboard.
        """
        from snipux.app import copy_image_to_clipboard

        copy_image_to_clipboard(self.rendered_image())

    # Subdirectory of ~/Pictures saves land in -- per the spec's "Save
    # writes a timestamped PNG to ~/Pictures/snipux, creating the
    # directory." `app.save_image`'s own default (bare ~/Pictures, used by
    # editor.py's still-existing Editor) doesn't know about this
    # subdirectory, so it's supplied here rather than changed there.
    SAVE_SUBDIRECTORY = "snipux"

    def save(self) -> Path:
        """Flatten the marks present *right now* onto the selection's crop
        and write it as a timestamped PNG under ~/Pictures/snipux, creating
        that directory if it doesn't exist yet. Returns the path written.
        """
        from snipux.app import save_image

        directory = Path.home() / "Pictures" / self.SAVE_SUBDIRECTORY
        return save_image(self.rendered_image(), directory)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # The ants only cost frames while actually on screen -- the
        # acceptance criterion is explicit that the timer must not keep
        # ticking (and scheduling repaints) once the overlay is hidden.
        self._ants_timer.start()
        # A selection set before this window was ever shown (e.g. a mode
        # that seeds one at construction time) needs the bar to catch up
        # now that `self.isVisible()` has actually become true.
        self._sync_bar_visibility()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._ants_timer.stop()
        self._bar.hide()
        self._tray.hide()

    def _advance_ants(self) -> None:
        """Advance the dashed stroke's offset by one animation frame.

        `ANTS_DASH` sums to the 14px one full dash-cycle the README's
        `stroke-dashoffset -> -14 over 700ms, linear` describes; each tick
        moves the offset by whatever fraction of that cycle one timer
        interval covers, so the total time for a full lap stays
        `ANTS_PERIOD_MS` regardless of the timer's own tick rate.
        """
        metric = design.tokens.Metric
        cycle = sum(metric.ANTS_DASH)
        step = cycle * self._ANTS_TIMER_INTERVAL_MS / metric.ANTS_PERIOD_MS
        self._dash_offset = (self._dash_offset + step) % cycle
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        handle = self._handle_at(event.position())
        if handle is None:
            if (
                self._eraser_active
                and self._selection is not None
                and QRectF(self._selection).contains(event.position())
            ):
                # No drag, per the spec's "Drawing": "eraser -- no drag." A
                # miss (nothing under the cursor) is already a safe no-op
                # inside erase_at itself.
                self.erase_at(event.position())
            # Every other tool's stroke-start is still a later ticket.
            # Falling through to this no-op -- rather than the handle
            # branch below -- is exactly what "stop event propagation at
            # the handle" needs from this method: a future stroke-start
            # only ever gets reached when a handle wasn't hit.
            return
        # Per the spec: a handle press is a resize, never a stroke, and
        # returning here means nothing past this point runs for it.
        self._active_handle = handle
        self._resize_anchor = QRect(self._selection)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._active_handle is not None:
            self._resize_selection(event.position())
            handle = self._active_handle
        else:
            handle = self._handle_at(event.position())
        if handle is not None:
            self.setCursor(_HANDLE_CURSORS[handle])
        elif self._selection is not None and QRectF(self._selection).contains(
            event.position()
        ):
            # Per docs/design/overlay-redesign.md's "Selection frame":
            # inside the selection, every tool shows a crosshair except the
            # eraser, which shows a pointer (SNX-38) -- so the cursor
            # itself reads as "click to remove" rather than "drag to draw."
            self.setCursor(
                Qt.CursorShape.PointingHandCursor
                if self._eraser_active
                else Qt.CursorShape.CrossCursor
            )
        else:
            self.unsetCursor()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._active_handle = None
        self._resize_anchor = None

    def _resize_selection(self, pos: QPointF) -> None:
        """Apply one drag-move of `self._active_handle` to the selection.

        `self._resize_anchor` is the selection as it stood when the drag
        started; edges this handle doesn't free are read from it and never
        written below, which is what keeps the opposite edge/corner
        anchored for the whole drag. Clamps are applied in the order the
        README's "Re-framing" section gives -- minimum size, `x >= 0`,
        `y >= 52`, stays inside the window, room for the floating bar --
        with the one deliberate deviation the ticket calls for: the
        minimum is `tokens.Metric.SEL_MIN_W/H` (16x16, not the spec's
        200x140), and the floating-bar clamp gives way to that minimum
        instead of the other way round.
        """
        handle = self._active_handle
        anchor = QRectF(self._resize_anchor)
        metric = design.tokens.Metric

        left, top = anchor.left(), anchor.top()
        right, bottom = anchor.right(), anchor.bottom()
        free_left = handle in _LEFT_HANDLES
        free_right = handle in _RIGHT_HANDLES
        free_top = handle in _TOP_HANDLES
        free_bottom = handle in _BOTTOM_HANDLES

        # 1. Minimum size: the dragged edge stops MIN away from the anchor
        # edge it's measured against, which never itself moves.
        if free_left:
            left = min(pos.x(), right - metric.SEL_MIN_W)
        if free_right:
            right = max(pos.x(), left + metric.SEL_MIN_W)
        if free_top:
            top = min(pos.y(), bottom - metric.SEL_MIN_H)
        if free_bottom:
            bottom = max(pos.y(), top + metric.SEL_MIN_H)

        # 2. x >= 0
        if free_left:
            left = max(left, 0.0)
        # 3. y >= 52, clear of the top hint HUD.
        if free_top:
            top = max(top, self._TOP_CLEARANCE)
        # 4. Stays inside the window.
        if free_right:
            right = min(right, self.width())
        if free_bottom:
            bottom = min(bottom, self.height())
        # 5. Room for the floating bar below. height <= window_height - y -
        # BAR_ROOM is, since height is always bottom - top, the same bound
        # as bottom <= window_height - BAR_ROOM regardless of y -- but only
        # tightens the bottom edge when doing so wouldn't undercut the
        # minimum height step 1 already established (the ticket's
        # deliberate reordering of the spec's clamps).
        if free_bottom:
            bar_limit = self.height() - self._BAR_ROOM
            if bar_limit >= top + metric.SEL_MIN_H:
                bottom = min(bottom, bar_limit)

        self.set_selection(
            QRect(round(left), round(top), round(right - left), round(bottom - top))
        )

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        # Layer 1: the frozen frame, full window. This is also what keeps
        # the selection "undimmed and at 1:1": the scrim below punches a
        # hole out of this exact drawImage call rather than compositing a
        # second one, so the hole shows precisely these pixels, untouched.
        painter.drawImage(QRectF(self.rect()), self._frame.image)
        self._paint_scrim(painter)
        if self._selection is not None:
            # Ink before the stroke/handles, matching the design's layer
            # order within the selection: "undimmed pixmap, ink layer,
            # frame stroke, handles, chips".
            self._paint_marks(painter)
            # Smooths the dashed diagonal-adjacent stroke and the rounded
            # bracket/handle corners; the scrim above is a flat axis-aligned
            # fill and doesn't need it.
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            self._paint_selection_stroke(painter)
            self._paint_corner_brackets(painter)
            self._paint_edge_handles(painter)
        painter.end()

    def _paint_marks(self, painter: QPainter) -> None:
        """The ink layer: `_marks`, clipped to the selection.

        Marks are stored in this widget's own window coordinates -- the
        same space `_selection` lives in (see the class docstring) -- so a
        re-frame never touches a mark's own points, only where the clip
        rect sits over them. A mark whose points fall outside the clip
        simply isn't painted this frame; it stays in `_marks` and reappears
        the instant the selection grows back over it. Per
        docs/design/overlay-redesign.md's "Ink lives in screen
        coordinates": "in Qt just `painter.setClipRect(sel)` before drawing
        marks."
        """
        if not self._marks:
            return
        painter.save()
        painter.setClipRect(QRectF(self._selection))
        step_counter = 0
        for shape in self._marks:
            if isinstance(shape, StepMarker):
                step_counter += 1
                shape.number = step_counter
            if isinstance(shape, ObscuringShape):
                # Obscuring marks sample the frozen frame's own pixels
                # rather than paint onto a painter (see
                # shapes.ObscuringShape.draw()) -- compositing those live
                # against a resizable selection is a later ticket's
                # concern, same as the tool that would create them.
                # render_selection() (export) still handles them correctly,
                # via render()'s own special-casing.
                continue
            shape.draw(painter)
        painter.restore()

    def _paint_scrim(self, painter: QPainter) -> None:
        """Layer 2: dim everything outside the selection.

        Painted here, in this widget's own paintEvent, rather than as a
        translucent child widget stacked over the whole window -- per the
        spec, a full-window child would sit above the (future) ink layer in
        z-order and eat its mouse events. A single even-odd fill dims the
        window and punches the selection out in one call, so there's no
        separate "dim then punch a hole" step that could disagree with this
        one at the selection's edge.
        """
        widget_rect = QRectF(self.rect())
        path = QPainterPath()
        path.addRect(widget_rect)
        if self._selection is not None:
            local_selection = QRectF(self._selection).intersected(widget_rect)
            if not local_selection.isEmpty():
                path.addRect(local_selection)
        path.setFillRule(Qt.FillRule.OddEvenFill)
        # design.color() resolves tokens.Color.DIM + DIM_ALPHA together, per
        # its own docstring's "a colour and its alpha are never applied
        # separately" -- the literal is not re-typed here.
        painter.fillPath(path, design.color("DIM"))

    # -- selection frame: stroke -----------------------------------------

    def _stroke_pens(self) -> tuple[QPen, QPen]:
        """The two coincident 1px pens the selection rect is stroked with:
        a plain solid one underneath an animated dashed one.

        Per the README: "two coincident strokes... which reads as motion
        without either stroke dominating." Split out from the paint call so
        the dash pattern and colours -- the acceptance criterion's "using
        the dash pattern from tokens.py" -- can be asserted on directly
        instead of only inferred from rendered pixels.
        """
        metric = design.tokens.Metric
        solid = QPen(design.color("SEL_STROKE"))
        solid.setWidthF(metric.SEL_STROKE_W)

        dashed = QPen(design.color("SEL_ANTS"))
        dashed.setWidthF(metric.SEL_STROKE_W)
        dashed.setDashPattern(list(metric.ANTS_DASH))
        dashed.setDashOffset(self._dash_offset)
        return solid, dashed

    def _paint_selection_stroke(self, painter: QPainter) -> None:
        # Inset half a pixel, like the reference's `x=0.5 y=0.5` rect, so a
        # 1px pen lands on a crisp pixel line instead of straddling two.
        rect = QRectF(self._selection).adjusted(0.5, 0.5, -0.5, -0.5)
        solid, dashed = self._stroke_pens()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(solid)
        painter.drawRect(rect)
        painter.setPen(dashed)
        painter.drawRect(rect)

    # -- selection frame: corner brackets / handles ------------------------

    def _bracket_path(self, handle: Handle) -> QPainterPath:
        """The filled L-bracket for one corner, in this widget's local
        (window) coordinates.

        Built by mirroring the canonical top-left bracket from
        `_l_bracket_local_path` with a `QTransform`, rather than re-deriving
        each corner's outline by hand -- the four corners are the same
        shape reflected across one or both axes and anchored to that
        corner's point on the selection rect, offset outward by
        `_CORNER_BRACKET_OFFSET` so the bracket straddles the 1px stroke.
        """
        # QRectF, not the raw QRect `self._selection` -- QRect.right()/
        # bottom() are inclusive (left + width - 1), which would throw every
        # offset below off by one pixel against the QRectF math everywhere
        # else in this class.
        sel = QRectF(self._selection)
        metric = design.tokens.Metric
        local = _l_bracket_local_path(
            metric.CORNER_LEN, metric.CORNER_W, self._CORNER_BRACKET_RADIUS
        )

        flip_x = handle in (Handle.TOP_RIGHT, Handle.BOTTOM_RIGHT)
        flip_y = handle in (Handle.BOTTOM_LEFT, Handle.BOTTOM_RIGHT)
        off = self._CORNER_BRACKET_OFFSET
        anchor_x = sel.right() + off if flip_x else sel.left() - off
        anchor_y = sel.bottom() + off if flip_y else sel.top() - off

        transform = QTransform()
        transform.translate(anchor_x, anchor_y)
        transform.scale(-1 if flip_x else 1, -1 if flip_y else 1)
        return transform.map(local)

    def _edge_handle_rect(self, handle: Handle) -> QRectF:
        """The rounded bar for one edge handle, in local (window)
        coordinates: `HANDLE_LONG x HANDLE_SHORT` (or transposed for a
        vertical edge), centred on the edge and offset outward by
        `_EDGE_HANDLE_OFFSET` so it overhangs the stroke.
        """
        sel = QRectF(self._selection)  # see the QRectF note in _bracket_path
        metric = design.tokens.Metric
        long_, short = metric.HANDLE_LONG, metric.HANDLE_SHORT
        off = self._EDGE_HANDLE_OFFSET
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

    def _corner_hit_rect(self, handle: Handle) -> QRectF:
        """The invisible `HANDLE_HIT`-square hit target for one corner, in
        local (window) coordinates -- offset outward by
        `_CORNER_HIT_OFFSET` and centred on the corner point, same as the
        bracket it sits under. No chrome of its own: the bracket is the
        visible handle, per the README.
        """
        sel = QRectF(self._selection)  # see the QRectF note in _bracket_path
        size = design.tokens.Metric.HANDLE_HIT
        off = self._CORNER_HIT_OFFSET

        if handle is Handle.TOP_LEFT:
            return QRectF(sel.left() - off, sel.top() - off, size, size)
        if handle is Handle.TOP_RIGHT:
            return QRectF(sel.right() - size + off, sel.top() - off, size, size)
        if handle is Handle.BOTTOM_LEFT:
            return QRectF(sel.left() - off, sel.bottom() - size + off, size, size)
        if handle is Handle.BOTTOM_RIGHT:
            return QRectF(sel.right() - size + off, sel.bottom() - size + off, size, size)
        raise ValueError(f"not a corner handle: {handle!r}")

    def _handle_at(self, pos: QPointF) -> Handle | None:
        """Which handle (if any) a local-coordinate point falls in.

        Corners checked first: a very small selection could bring a corner
        hit target and an edge handle's rect into overlap, and the corner
        bracket is the more specific, visually on-top target of the two.
        """
        if self._selection is None:
            return None
        for handle in _CORNER_HANDLES:
            if self._corner_hit_rect(handle).contains(pos):
                return handle
        for handle in _EDGE_HANDLES:
            if self._edge_handle_rect(handle).contains(pos):
                return handle
        return None

    def _paint_corner_brackets(self, painter: QPainter) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(design.color("HANDLE"))
        for handle in _CORNER_HANDLES:
            painter.drawPath(self._bracket_path(handle))

    def _paint_edge_handles(self, painter: QPainter) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(design.color("HANDLE"))
        for handle in _EDGE_HANDLES:
            rect = self._edge_handle_rect(handle)
            painter.drawRoundedRect(rect, self._EDGE_HANDLE_RADIUS, self._EDGE_HANDLE_RADIUS)


def create_overlays(
    frame: Frame,
    monitor_geometries: list[QRectF],
    mode: SelectionMode = SelectionMode.RECTANGLE,
    geometry_provider: GeometryProvider | None = None,
) -> list[Overlay]:
    """Build one `Overlay` per monitor geometry.

    Geometries are absolute logical virtual-desktop rects, the same space
    `Frame` uses. Does not touch `QApplication.screens()` or show any
    window — the caller is responsible for sourcing real geometries and
    showing the windows, which keeps this module testable with synthetic
    geometries offscreen.

    A single `Overlay` only knows its own monitor's geometry, but "full
    screen" means the union of every monitor, so that union is computed once
    here and handed to each `Overlay` as `virtual_desktop_rect`.
    """
    virtual_desktop_rect = None
    for geometry in monitor_geometries:
        virtual_desktop_rect = (
            geometry if virtual_desktop_rect is None else virtual_desktop_rect.united(geometry)
        )
    return [
        Overlay(
            frame,
            geometry,
            mode=mode,
            geometry_provider=geometry_provider,
            virtual_desktop_rect=virtual_desktop_rect,
        )
        for geometry in monitor_geometries
    ]
