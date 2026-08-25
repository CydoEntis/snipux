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
from typing import Callable

from PyQt6.QtCore import Qt, QPoint, QPointF, QRect, QRectF, QSize, QSizeF, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QIcon,
    QImage,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QScreen,
    QTransform,
)
from PyQt6.QtWidgets import (
    QApplication,
    QColorDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from snipux import design
from snipux.capture import BackendRegistry, CaptureError, Frame
from snipux.shapes import (
    Arrow,
    Blur,
    Crop,
    Highlighter,
    ObscuringShape,
    Pen,
    Pixelate,
    Rectangle,
    Shape,
    StepMarker,
    Text,
    finalize_mark,
    next_step_number,
    render_selection,
)


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

    def _current_selection_path(self) -> QPainterPath | None:
        """The lasso path the veil should invert against right now, in
        absolute logical coordinates -- the live drag path while one is in
        progress, the confirmed path once released, or `None` outside
        freeform mode (or before any freeform drag has started), in which
        case `_paint_veil` falls back to the plain bounding-rect hole every
        other mode already uses.
        """
        if self._mode is not SelectionMode.FREEFORM:
            return None
        return self._drag_path if self._drag_path is not None else self._selection_path

    def _paint_veil(self, painter: QPainter) -> None:
        widget_rect = QRectF(self.rect())
        # A single even-odd fill dims everywhere except the selection hole
        # in one call, so there's no separate "dim then punch a hole" step
        # that could disagree with this one at the edge.
        path = QPainterPath()
        path.addRect(widget_rect)
        selection_path = self._current_selection_path()
        if selection_path is not None:
            # Freeform: the scrim inverts against the traced lasso itself,
            # not its bounding box, per docs/design/overlay-redesign.md's
            # "Capture modes" entry for Freeform. Translated the same way
            # `_to_local` translates a rect -- this widget's own local
            # origin is this monitor's absolute top-left -- so a path drawn
            # partly off this monitor still punches the right hole in this
            # Overlay's own slice of it; QPainter clips the rest for free.
            origin = self._monitor_geometry.topLeft()
            path.addPath(selection_path.translated(-origin))
        elif self._selection is not None:
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

# tokens.SHORTCUTS keyed by Qt.Key code rather than letter (SNX-47), so
# OverlayWindow.keyPressEvent can look a QKeyEvent.key() up directly instead
# of going through event.text() -- which depends on locale/shift state in a
# way a letter's key code never does.
_SHORTCUT_KEY_CODES = {
    getattr(Qt.Key, f"Key_{letter}"): tool for letter, tool in design.tokens.SHORTCUTS.items()
}


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

        self._text_label = QLabel(text, self)
        text_label = self._text_label
        text_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        font = QFont(design.font_families().ui)
        size, weight = design.tokens.Font.CHIP_LABEL
        font.setPixelSize(round(size))
        font.setWeight(QFont.Weight(weight))
        text_label.setFont(font)
        text_label.setStyleSheet(f"color: {text_color.name()};")

        for widget in (text_label, icon_label) if icon_after else (icon_label, text_label):
            layout.addWidget(widget)

    def set_text(self, text: str) -> None:
        """Update the pill's own label -- used by the capture chip (SNX-44)
        to name whichever mode the popover has selected, per the
        reference's `{{ mode }}` binding on the chip button itself. Save's
        own `_PillButton` never calls this; its label is fixed.
        """
        self._text_label.setText(text)
        self.updateGeometry()

    def sizeHint(self) -> QSize:
        # QPushButton.sizeHint() measures `self.text()`/`self.icon()` --
        # unused here, since the icon+label pairing lives in the child
        # QHBoxLayout instead (see class docstring), so the base
        # implementation falls back to a placeholder "XXXX" string in the
        # button's own default font. That placeholder is what clipped the
        # capture chip and Save down to a few px of label regardless of the
        # word actually on screen (SNX-59): a fixed-looking width that
        # wasn't derived from either the real text or the real font. The
        # child layout already knows the true width, because it was built
        # from `pad_left`/`pad_right`/spacing plus each child's own
        # sizeHint -- and the text label's sizeHint comes from the font
        # it's actually rendering in, fallback or not.
        return self.layout().sizeHint()

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()


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

    # -- capture mode (SNX-44) --------------------------------------------

    def set_capture_mode(self, label: str) -> None:
        """Update the chip's own label to `label`, per the reference's
        `{{ mode }}` binding on the chip button itself -- the chip always
        names whichever capture mode is current, not just its own "Region"
        default from `_build_capture_chip`.
        """
        self._chip.set_text(label)

    # -- tool selection --------------------------------------------------

    def _on_tool_clicked(self, tool: str) -> None:
        self.select_tool(tool)

    def select_tool(self, tool: str) -> None:
        """Public equivalent of clicking `tool`'s own button: the same
        active-tool bookkeeping and `toolSelected` emission a click
        produces. `OverlayWindow.keyPressEvent`'s tool-letter shortcuts
        (SNX-47) call this rather than reaching for the private
        `_on_tool_clicked` -- a shortcut and a click are two ways to reach
        the same state change, not two copies of it that could drift apart.
        """
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
# names the tools that get *this* tray; blur gets a different one --
# `BlurTray` below (SNX-42) -- and the eraser gets none at all.


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
    tray is `BlurTray` below, and the eraser, which gets no tray at all)
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

    # -- fill ------------------------------------------------------------

    def paintEvent(self, event) -> None:
        # Same glass treatment as FloatingBar.paintEvent -- design.color's
        # BAR_BG/BAR_BORDER already carry their alphas (93%/10%), painted
        # here as a translucent fill+stroke rather than reduced widget
        # opacity, so every child on top (swatches, slider, readout,
        # preview dot, hint) stays fully opaque, per SNX-61.
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        metric = design.tokens.Metric
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(design.color("BAR_BG"))
        painter.drawRoundedRect(rect, metric.TRAY_RADIUS, metric.TRAY_RADIUS)

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(design.color("BAR_BORDER"))
        painter.drawRoundedRect(rect, metric.TRAY_RADIUS, metric.TRAY_RADIUS)
        painter.end()

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


# ---------------------------------------------------------------------------
# Blur tray (SNX-42)
# ---------------------------------------------------------------------------
# docs/design/overlay-redesign.md's "Settings tray" section, "Blur tray"
# paragraph, is the authority here. It replaces `SettingsTray` outright
# rather than sitting alongside it -- neither colour nor stroke means
# anything to an obscuring shape -- with a two-segment Blur/Pixelate toggle,
# a strength slider/readout and the tool hint. The active segment is the
# state that decides which of shapes.py's two `ObscuringShape` subclasses a
# blur drag commits, once that drag-handling itself lands in a later ticket
# -- mirroring how `SettingsTray`'s colour/stroke already sit unread by any
# drawing code until their own tool wiring arrives (see `OverlayWindow`'s
# docstring).


class _SegmentButton(QPushButton):
    """One half of the blur tray's Blur/Pixelate toggle, per the reference's
    computed `blurBg`/`blurFg` (and `pixBg`/`pixFg`) pair: transparent fill
    and `ICON_IDLE` text when idle, `ICON_ACTIVE_BG` fill and `ICON_ACTIVE`
    text when this segment is the tray's current `blur_mode`. A flat
    `QPushButton` recoloured by stylesheet, the same approach
    `_CustomColorButton` already uses for its single-state border -- there's
    only ever one fill/text pair active at a time here, so a hand-painted
    `paintEvent` (as `_SwatchButton` needs for its two-ring selected state)
    would be more machinery than this control needs.
    """

    # Prose-only literals from the reference's inline styles for the two
    # segment buttons ("padding:6px 11px; border-radius:6px; font:500
    # 11.5px") -- not tokens.Metric/Font entries, same convention
    # OverlayWindow's own _CORNER_BRACKET_OFFSET and friends already follow.
    _RADIUS = 6
    _PAD_V = 6
    _PAD_H = 11
    _FONT_PX = 11.5
    _FONT_WEIGHT = 500

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setFlat(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        font = QFont(design.font_families().ui)
        font.setPixelSize(round(self._FONT_PX))
        font.setWeight(QFont.Weight(self._FONT_WEIGHT))
        self.setFont(font)
        self._active = False
        self._refresh()

    @property
    def is_active(self) -> bool:
        return self._active

    def set_active(self, active: bool) -> None:
        self._active = active
        self._refresh()

    def _refresh(self) -> None:
        if self._active:
            bg = design.color("ICON_ACTIVE_BG")
            fg = design.color("ICON_ACTIVE")
        else:
            bg = QColor(0, 0, 0, 0)
            fg = design.color("ICON_IDLE")
        self.setStyleSheet(
            "QPushButton { border: none; border-radius: %dpx; padding: %dpx %dpx;"
            " background: rgba(%d, %d, %d, %s); color: %s; }"
            % (
                self._RADIUS,
                self._PAD_V,
                self._PAD_H,
                bg.red(),
                bg.green(),
                bg.blue(),
                bg.alphaF(),
                fg.name(),
            )
        )


class _BlurModeWell(QWidget):
    """The inset well the two segment buttons sit in -- `#000000` at 35%
    alpha, radius 8, 2px padding and gap, per the spec's "in a #000000 35%
    inset well." Paints its own translucent fill the same way `_ToolPill`
    does, rather than a stylesheet, since a stylesheet fill here would also
    have to survive being a parent of two more heavily-styled children.
    """

    _BG_ALPHA = 0.35
    _RADIUS = 8
    _PAD = 2
    _GAP = 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(self._PAD, self._PAD, self._PAD, self._PAD)
        layout.setSpacing(self._GAP)

        self.blur_button = _SegmentButton("Blur", self)
        layout.addWidget(self.blur_button)
        self.pixelate_button = _SegmentButton("Pixelate", self)
        layout.addWidget(self.pixelate_button)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg = QColor("#000000")
        bg.setAlphaF(self._BG_ALPHA)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg)
        painter.drawRoundedRect(QRectF(self.rect()), self._RADIUS, self._RADIUS)
        painter.end()


class BlurTray(QWidget):
    """The overlay redesign's blur tray: the Blur/Pixelate toggle, a
    strength slider/readout and the tool hint, per
    docs/design/overlay-redesign.md's "Blur tray" paragraph.

    Shown in place of `SettingsTray` -- never alongside it -- while the
    bar's active tool is `'blur'`; see `OverlayWindow._sync_tray_visibility`.
    Unlike `SettingsTray`, there is only ever one tool this tray applies to,
    so it carries no `set_tool` of its own.
    """

    blurModeChanged = pyqtSignal(str)
    strengthChanged = pyqtSignal(int)

    # The reference's strength readout is a narrower `min-width:20px` than
    # SettingsTray's own 34px -- a plain number ("8") never runs as wide as
    # a stroke's "26px" -- so this is its own literal, not
    # SettingsTray._READOUT_MIN_W reused.
    _READOUT_MIN_W = 20

    # The "Strength" label's own font, per the reference's "font:400 11px" --
    # distinct from tokens.Font.TRAY_HINT (11.5px), which the hint text at
    # the tray's far end still uses.
    _STRENGTH_LABEL_PX = 11
    _STRENGTH_LABEL_WEIGHT = 400

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        metric = design.tokens.Metric
        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            metric.TRAY_PAD_H, metric.TRAY_PAD_V, metric.TRAY_PAD_H, metric.TRAY_PAD_V
        )
        layout.setSpacing(metric.TRAY_GAP)

        # No dividers in this tray, unlike SettingsTray -- the reference's
        # own isBlur markup never places one between the well, the strength
        # controls and the hint, just the same 12px flex gap throughout.
        self._blur_mode: str = "blur"
        self._strength: int = metric.BLUR_DEFAULT

        self._well = _BlurModeWell(self)
        self._well.blur_button.clicked.connect(lambda: self.set_blur_mode("blur"))
        self._well.pixelate_button.clicked.connect(lambda: self.set_blur_mode("pix"))
        layout.addWidget(self._well)

        self._strength_label = QLabel("Strength", self)
        label_font = QFont(design.font_families().ui)
        label_font.setPixelSize(self._STRENGTH_LABEL_PX)
        label_font.setWeight(QFont.Weight(self._STRENGTH_LABEL_WEIGHT))
        self._strength_label.setFont(label_font)
        self._strength_label.setStyleSheet(f"color: {design.color('TEXT_MUTED').name()};")
        layout.addWidget(self._strength_label)

        self._slider = QSlider(Qt.Orientation.Horizontal, self)
        self._slider.setRange(metric.BLUR_MIN, metric.BLUR_MAX)
        self._slider.setValue(self._strength)
        self._slider.setFixedWidth(metric.SLIDER_W)
        self._slider.valueChanged.connect(self.set_strength)
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

        self._hint = QLabel(self)
        hint_font = QFont(design.font_families().ui)
        size, weight = design.tokens.Font.TRAY_HINT
        hint_font.setPixelSize(round(size))
        hint_font.setWeight(QFont.Weight(weight))
        self._hint.setFont(hint_font)
        self._hint.setStyleSheet(f"color: {design.color('TEXT_MUTED').name()};")
        self._hint.setText(design.tokens.TOOL_HINTS.get("blur", ""))
        layout.addWidget(self._hint)

        self._select_segment(self._blur_mode)
        self._refresh_readout()

    # -- fill ------------------------------------------------------------

    def paintEvent(self, event) -> None:
        # Same panel treatment as SettingsTray.paintEvent -- see that
        # method's docstring for why the fill/border are painted as a
        # translucent brush rather than widget opacity, per SNX-61.
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        metric = design.tokens.Metric
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(design.color("BAR_BG"))
        painter.drawRoundedRect(rect, metric.TRAY_RADIUS, metric.TRAY_RADIUS)

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(design.color("BAR_BORDER"))
        painter.drawRoundedRect(rect, metric.TRAY_RADIUS, metric.TRAY_RADIUS)
        painter.end()

    # -- state ---------------------------------------------------------

    @property
    def blur_mode(self) -> str:
        return self._blur_mode

    @property
    def strength(self) -> int:
        return self._strength

    def set_blur_mode(self, mode: str) -> None:
        """Set the active segment -- `'blur'` or `'pix'` -- deselecting the
        other one so exactly one always reads as active, per the spec's
        "exactly one segment reads as active." This is the state that
        decides which of shapes.py's `Blur`/`Pixelate` a drag commits, per
        the spec's "the toggle chooses which of the two obscuring shapes a
        drag commits."
        """
        self._blur_mode = mode
        self._select_segment(mode)
        self.blurModeChanged.emit(mode)

    def set_strength(self, strength: int) -> None:
        """Set the current blur strength, clamped to `tokens.Metric`'s
        `BLUR_MIN`/`BLUR_MAX` range, and refresh the readout.
        """
        metric = design.tokens.Metric
        strength = max(metric.BLUR_MIN, min(strength, metric.BLUR_MAX))
        self._strength = strength
        if self._slider.value() != strength:
            self._slider.setValue(strength)
        self._refresh_readout()
        self.strengthChanged.emit(strength)

    def _select_segment(self, mode: str) -> None:
        self._well.blur_button.set_active(mode == "blur")
        self._well.pixelate_button.set_active(mode == "pix")

    def _refresh_readout(self) -> None:
        self._readout.setText(str(self._strength))


# ---------------------------------------------------------------------------
# Capture-mode popover (SNX-44)
# ---------------------------------------------------------------------------
# docs/design/overlay-redesign.md's "Capture-mode popover" section is the
# authority here, cross-checked against the reference's own `renderVals()`
# (`menuUp = barTop > 300`, `cycleDelay`, `modes.map`) for anything the prose
# leaves implicit -- e.g. that the chip's own label follows `st.mode`, not
# just its "Region" construction default. The chip is a mode selector, not
# an action: per the ticket, only Region does anything past recording the
# pick -- Window, Full screen and Freeform are separate tickets that will
# read `OverlayWindow._capture_mode` once they land.


class _CaptureModeRow(QPushButton):
    """One row of the popover: glyph, a two-line label/note, and a check
    mark for whichever mode is currently selected, per the spec's rows
    bullet. Hand-painted background -- like `_SwatchButton`'s ring -- since
    a hovered *and* selected row needs the hover fill to win, which a
    stylesheet's static rule can't express.
    """

    _ICON_SIZE = 16
    _CHECK_SIZE = 15
    _GAP = 10
    _LABEL_GAP = 2
    # Prose-only literals from the spec's "hover `#ffffff` at 9%" /
    # "Selected row background `#ffffff` at 8%" -- not tokens.Color entries,
    # same convention `_ToolPill._BG_ALPHA`/`_BlurModeWell._BG_ALPHA` already
    # follow for a one-off fill no other control shares.
    _HOVER_BG_ALPHA = 0.09
    _SELECTED_BG_ALPHA = 0.08

    def __init__(self, mode_label: str, icon_name: str, note: str, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFlat(True)
        self.setStyleSheet("QPushButton { border: none; background: transparent; }")

        self._icon_name = icon_name
        self._selected = False
        self._bg_alpha: float | None = None

        metric = design.tokens.Metric
        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            metric.MENU_ROW_PAD_H,
            metric.MENU_ROW_PAD_V,
            metric.MENU_ROW_PAD_H,
            metric.MENU_ROW_PAD_V,
        )
        layout.setSpacing(self._GAP)

        self._icon_label = QLabel(self)
        self._icon_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self._icon_label)

        text_column = QVBoxLayout()
        text_column.setContentsMargins(0, 0, 0, 0)
        text_column.setSpacing(self._LABEL_GAP)

        self._label = QLabel(mode_label, self)
        self._label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        label_font = QFont(design.font_families().ui)
        size, weight = design.tokens.Font.MENU_LABEL
        label_font.setPixelSize(round(size))
        label_font.setWeight(QFont.Weight(weight))
        self._label.setFont(label_font)
        text_column.addWidget(self._label)

        note_label = QLabel(note, self)
        note_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        note_font = QFont(design.font_families().ui)
        size, weight = design.tokens.Font.MENU_NOTE
        note_font.setPixelSize(round(size))
        note_font.setWeight(QFont.Weight(weight))
        note_label.setFont(note_font)
        note_label.setStyleSheet(f"color: {design.color('TEXT_MUTED').name()};")
        text_column.addWidget(note_label)

        layout.addLayout(text_column, 1)

        self._check = QLabel(self)
        self._check.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._check.setPixmap(
            design.icon("check", design.color("ACCENT")).pixmap(
                self._CHECK_SIZE, self._CHECK_SIZE
            )
        )
        self._check.setVisible(False)
        layout.addWidget(self._check)

        self._refresh()

    @property
    def is_selected(self) -> bool:
        return self._selected

    def set_selected(self, selected: bool) -> None:
        """Mark this row as the current capture mode -- the check glyph and
        the `ICON_ACTIVE` label/icon colour both follow `_selected`, per
        the spec's "a check glyph... for the selected row."
        """
        self._selected = selected
        self._refresh()

    def _refresh(self, hovered: bool | None = None) -> None:
        if hovered is None:
            hovered = self.underMouse()
        # Hover wins over the selected fill when both apply -- every row,
        # including the current mode's own, still needs to read as
        # clickable while the pointer is over it.
        if hovered:
            self._bg_alpha = self._HOVER_BG_ALPHA
        elif self._selected:
            self._bg_alpha = self._SELECTED_BG_ALPHA
        else:
            self._bg_alpha = None

        fg = design.color("ICON_ACTIVE") if self._selected else design.color("ICON_IDLE")
        self._label.setStyleSheet(f"color: {fg.name()};")
        self._icon_label.setPixmap(
            design.icon(self._icon_name, fg).pixmap(self._ICON_SIZE, self._ICON_SIZE)
        )
        self._check.setVisible(self._selected)
        self.update()

    def enterEvent(self, event) -> None:
        self._refresh(hovered=True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._refresh(hovered=False)
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        if self._bg_alpha is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg = QColor("#ffffff")
        bg.setAlphaF(self._bg_alpha)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg)
        metric = design.tokens.Metric
        painter.drawRoundedRect(
            QRectF(self.rect()), metric.MENU_ROW_RADIUS, metric.MENU_ROW_RADIUS
        )
        painter.end()


class _DelayRow(QPushButton):
    """The popover's delay row: a timer glyph, the word "Delay", and the
    current value right-aligned -- per the spec's "Delay" paragraph. No
    selected state (unlike `_CaptureModeRow`): the row itself *is* the
    control, and there is nothing else in the popover it could read as
    selected relative to.
    """

    _ICON_SIZE = 16
    _GAP = 10
    _HOVER_BG_ALPHA = 0.09  # same one-off fill _CaptureModeRow's hover uses
    # "mono 11.5px `#8f9689`" -- the closest tokens.Font entry, MENU_NOTE,
    # is 11.0px and already spoken for by the mode rows' own notes, so this
    # stays a local literal per `BlurTray._STRENGTH_LABEL_PX`'s convention.
    _VALUE_PX = 11.5
    _VALUE_WEIGHT = 400

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFlat(True)
        self.setStyleSheet("QPushButton { border: none; background: transparent; }")

        self._hovered = False

        metric = design.tokens.Metric
        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            metric.MENU_ROW_PAD_H,
            metric.MENU_ROW_PAD_V,
            metric.MENU_ROW_PAD_H,
            metric.MENU_ROW_PAD_V,
        )
        layout.setSpacing(self._GAP)

        icon_label = QLabel(self)
        icon_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        icon_label.setPixmap(
            design.icon("timer", design.color("ICON_IDLE")).pixmap(
                self._ICON_SIZE, self._ICON_SIZE
            )
        )
        layout.addWidget(icon_label)

        label = QLabel("Delay", self)
        label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        label_font = QFont(design.font_families().ui)
        size, weight = design.tokens.Font.MENU_LABEL
        label_font.setPixelSize(round(size))
        label_font.setWeight(QFont.Weight(weight))
        label.setFont(label_font)
        label.setStyleSheet(f"color: {design.color('ICON_IDLE').name()};")
        layout.addWidget(label, 1)

        self._value = QLabel(self)
        self._value.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        value_font = QFont(design.font_families().mono)
        value_font.setPixelSize(round(self._VALUE_PX))
        value_font.setWeight(QFont.Weight(self._VALUE_WEIGHT))
        self._value.setFont(value_font)
        self._value.setStyleSheet(f"color: {design.color('TEXT_MUTED').name()};")
        layout.addWidget(self._value)

    def set_value(self, text: str) -> None:
        self._value.setText(text)

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        if not self._hovered:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg = QColor("#ffffff")
        bg.setAlphaF(self._HOVER_BG_ALPHA)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg)
        metric = design.tokens.Metric
        painter.drawRoundedRect(
            QRectF(self.rect()), metric.MENU_ROW_RADIUS, metric.MENU_ROW_RADIUS
        )
        painter.end()


class _MenuSeparator(QWidget):
    """The 1px rule between the mode rows and the delay row, per the
    spec's "a 1px `#ffffff` 10% separator with 5px/4px margins." A fixed-
    height widget that paints its line inset from its own edges, mirroring
    how `_Divider` fixes its own size rather than leaning on layout
    margins for a hairline.
    """

    _MARGIN_V = 5
    _MARGIN_H = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(self._MARGIN_V * 2 + 1)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        # BAR_BORDER is the same #ffffff/10% pair the spec's separator
        # uses -- reused rather than re-typed, the same precedent
        # FloatingBar._build_save_button already sets for BAR_BORDER.
        painter.fillRect(
            self._MARGIN_H,
            self._MARGIN_V,
            self.width() - 2 * self._MARGIN_H,
            1,
            design.color("BAR_BORDER"),
        )
        painter.end()


class CaptureModePopover(QWidget):
    """The overlay redesign's capture-mode popover: `tokens.CAPTURE_MODES`
    as a list of rows, a separator, then the delay row -- per
    docs/design/overlay-redesign.md's "Capture-mode popover" section.

    A real child widget of `OverlayWindow`, built the same way
    `FloatingBar`/`SettingsTray` are -- opened and positioned by
    `OverlayWindow._toggle_capture_popover`, never painted in its own
    `paintEvent`. Picking a row only records the choice and closes the
    popover; Window, Full screen and Freeform don't do anything past that
    yet -- they're separate tickets in the same arc `_bar`'s tool buttons
    already follow (see `OverlayWindow._on_tool_selected`'s docstring).
    """

    modeSelected = pyqtSignal(str)
    delayChanged = pyqtSignal(str)

    # The README gives this literal directly ("if bar top > 300px") rather
    # than as a tokens.Metric entry -- same convention FloatingBar's own
    # _TOP_MAX_FROM_BOTTOM already follows for a prose-only constant.
    _UP_THRESHOLD = 300

    # Same #1a1c18 BAR_BG already names, at the popover's own 97% rather
    # than the bar's 93% -- no tokens.Color entry carries that exact alpha,
    # so it's a local literal rather than a one-off *_ALPHA sibling added
    # for a single caller.
    _BG_ALPHA = 0.97

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        metric = design.tokens.Metric
        # Fixed, not just flowed from its rows' natural width -- the spec
        # gives the popover an exact "262px wide," and `reposition` below
        # positions off that same literal rather than a sizeHint that could
        # drift from what actually gets painted.
        self.setFixedWidth(metric.MENU_W)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            metric.MENU_PAD, metric.MENU_PAD, metric.MENU_PAD, metric.MENU_PAD
        )
        layout.setSpacing(0)

        self._mode: str = design.tokens.CAPTURE_MODES[0][0]
        self._delay: str = design.tokens.DELAYS[0]

        self._rows: dict[str, _CaptureModeRow] = {}
        for label, icon_name, note in design.tokens.CAPTURE_MODES:
            row = _CaptureModeRow(label, icon_name, note, self)
            row.clicked.connect(lambda checked=False, m=label: self._on_row_clicked(m))
            self._rows[label] = row
            layout.addWidget(row)

        layout.addWidget(_MenuSeparator(self))

        self._delay_row = _DelayRow(self)
        self._delay_row.clicked.connect(self._on_delay_clicked)
        layout.addWidget(self._delay_row)

        self._select_row(self._mode)
        self._delay_row.set_value(self._delay)

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def delay(self) -> str:
        return self._delay

    def set_mode(self, mode: str) -> None:
        """Mark `mode`'s row checked without emitting `modeSelected` or
        closing the popover -- for a future caller seeding the popover from
        elsewhere, mirroring the split `FloatingBar.set_active_tool` keeps
        from its own `_on_tool_clicked`.
        """
        self._mode = mode
        self._select_row(mode)

    def _select_row(self, mode: str) -> None:
        for label, row in self._rows.items():
            row.set_selected(label == mode)

    def _on_row_clicked(self, mode: str) -> None:
        """Record `mode` and close the popover, per the spec's "picking a
        row records that mode and closes the popover." Modes past Region
        are separate tickets -- this never itself starts a window hover-
        highlight or a freeform lasso, only the recording.
        """
        self.set_mode(mode)
        self.modeSelected.emit(mode)
        self.hide()

    def _on_delay_clicked(self) -> None:
        """Cycle to the next `tokens.DELAYS` value, wrapping past the last
        back to the first -- per the spec's "Clicking cycles Off -> 3s ->
        5s -> 10s -> Off." Unlike a mode row, this leaves the popover open,
        mirroring the reference's own `cycleDelay`, which never touches
        `modeOpen`.
        """
        delays = design.tokens.DELAYS
        index = delays.index(self._delay)
        self._delay = delays[(index + 1) % len(delays)]
        self._delay_row.set_value(self._delay)
        self.delayChanged.emit(self._delay)

    def reposition(self, bar_geometry: QRect, window_size: QSize) -> None:
        """Position the popover against `bar_geometry` (`FloatingBar`'s own
        geometry, already in this widget's parent's coordinate space), per
        the spec's rule: "if bar top > 300px, place the popover at
        bar_top - popover_height - 8; otherwise place it below the bar."
        Horizontally centred on the bar and clamped inside `window_size`,
        mirroring `OverlayWindow._reposition_tray`'s own centring.
        """
        metric = design.tokens.Metric
        width = metric.MENU_W
        height = self.sizeHint().height()

        center_x = bar_geometry.center().x()
        left = center_x - width / 2
        left = max(0, min(left, window_size.width() - width))

        if bar_geometry.top() > self._UP_THRESHOLD:
            top = bar_geometry.top() - height - metric.MENU_OFFSET
        else:
            top = bar_geometry.bottom() + metric.MENU_OFFSET

        self.setGeometry(round(left), round(top), width, height)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        metric = design.tokens.Metric
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        bg = QColor(design.tokens.Color.BAR_BG)
        bg.setAlphaF(self._BG_ALPHA)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg)
        painter.drawRoundedRect(rect, metric.MENU_RADIUS, metric.MENU_RADIUS)

        # DIVIDER is the same #ffffff/12% pair the spec's popover border
        # uses -- reused rather than re-typed, the same precedent
        # FloatingBar._build_save_button sets for BAR_BORDER.
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(design.color("DIVIDER"))
        painter.drawRoundedRect(rect, metric.MENU_RADIUS, metric.MENU_RADIUS)
        painter.end()


# ---------------------------------------------------------------------------
# Top hint HUD (SNX-46)
# ---------------------------------------------------------------------------
# docs/design/overlay-redesign.md's "Top hint HUD" section is the authority
# here. It's a first-run affordance, behind a preference (`hints`) that
# defaults on -- see `OverlayWindow.set_hints_enabled` -- and the spec's own
# "Re-framing" section already reserves the room for it: `OverlayWindow.
# _TOP_CLEARANCE` (52) is 8px clear of `tokens.Metric.HUD_H` (44), so the two
# have agreed since SNX-33 landed and this ticket only has to paint into the
# space already held open.


class HintHUD(QWidget):
    """The overlay's full-width top hint bar, per docs/design/overlay-
    redesign.md's "Top hint HUD" section: `Esc discard ink · Enter copy &
    dismiss · P H A R S T B E pick a tool · drag any edge to re-frame -- the
    ink stays where you put it`.

    A real child widget of `OverlayWindow` -- built from real `QLabel`
    segments, not painted inside `OverlayWindow.paintEvent` -- the same
    reasoning `Toast` documents for itself: `OverlayWindow.rendered_image`
    flattens `_marks` onto the frame via `shapes.render_selection` and never
    touches this window's chrome at all, so the HUD can't leak into an
    export regardless of whether it happens to be on screen at the moment
    `copy`/`save` is called.

    Key names (`Esc`, `Enter`, the eight tool shortcuts) are their own
    labels in the mono family at `HUD_KEY` (pure white); the surrounding
    prose is the UI family at the muted `HUD_TEXT` -- per the spec's "Key
    names are mono in pure white," so a key reads as a key at a glance
    rather than blending into the sentence around it.
    """

    # (text, is_key) in reading order. The key segment for the tool
    # shortcuts is built from `_TOOL_SHORTCUT_KEYS`/`tokens.TOOLS` rather
    # than typed out as "P H A R S T B E" -- if a shortcut or tool order
    # ever changes, this line follows it instead of silently drifting out
    # of sync the way a hand-typed copy of the same letters could.
    def _segments(self) -> list[tuple[str, bool]]:
        keys = " ".join(_TOOL_SHORTCUT_KEYS[tool] for tool in design.tokens.TOOLS)
        return [
            ("Esc", True),
            (" discard ink · ", False),
            ("Enter", True),
            (" copy & dismiss · ", False),
            (keys, True),
            (
                " pick a tool · drag any edge to re-frame — the ink"
                " stays where you put it",
                False,
            ),
        ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedHeight(design.tokens.Metric.HUD_H)

        # Full-width and centred, per "Full width... contents centred" --
        # a stretch on both sides of the segment labels is what centres a
        # variable-width run of text without this widget needing to measure
        # it itself the way OverlayWindow's own chips do.
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addStretch(1)
        for text, is_key in self._segments():
            layout.addWidget(self._segment_label(text, is_key))
        layout.addStretch(1)

    def _segment_label(self, text: str, is_key: bool) -> QLabel:
        label = QLabel(text, self)
        label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        family = design.font_families().mono if is_key else design.font_families().ui
        font = QFont(family)
        size, weight = design.tokens.Font.HUD
        font.setPixelSize(round(size))
        font.setWeight(QFont.Weight(weight))
        label.setFont(font)
        colour = design.color("HUD_KEY" if is_key else "HUD_TEXT")
        label.setStyleSheet(f"color: {colour.name()};")
        return label

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        # A flat, translucent fill -- per the Qt notes' "cheaper fallback:
        # raise the fill alpha... and skip the blur," the same trade-off
        # FloatingBar/SettingsTray/CaptureModePopover already make for their
        # own backdrop-filter blur.
        painter.fillRect(QRectF(self.rect()), design.color("HUD_BG"))
        painter.end()


# ---------------------------------------------------------------------------
# Toast (SNX-45)
# ---------------------------------------------------------------------------


class Toast(QWidget):
    """The overlay redesign's toast: bottom centre, above everything, per
    docs/design/overlay-redesign.md's "Toast" section.

    A real child widget of `OverlayWindow`, built and positioned the same
    way `FloatingBar`/`SettingsTray`/`CaptureModePopover` are -- never
    painted inside `OverlayWindow.paintEvent`, which is what the spec means
    by "chrome painted over the overlay rather than something drawn into
    the frame": `OverlayWindow.rendered_image` flattens `_marks` onto the
    frame's own pixmap via `shapes.render_selection` and never touches this
    widget (or any other chrome) at all, so a toast can never end up in an
    export regardless of whether it happens to be on screen at the moment
    `copy`/`save` is called.

    There is only ever one toast, per the spec's "a new toast replaces the
    old one and restarts the timer" -- `show_message` overwrites whatever
    the previous call was showing and restarts `_timer` rather than a
    caller stacking a second widget, since this class is itself the single
    instance `OverlayWindow` keeps as `_toast`.
    """

    _ICON_SIZE = 15
    # Prose-only spacing between the glyph and the message, like
    # `_ToolPill`/`_FROZEN_INNER_GAP`'s own un-tokenized gaps elsewhere in
    # this file.
    _GAP = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        metric = design.tokens.Metric

        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            metric.TOAST_PAD_H, metric.TOAST_PAD_V, metric.TOAST_PAD_H, metric.TOAST_PAD_V
        )
        layout.setSpacing(self._GAP)

        self._icon_label = QLabel(self)
        self._icon_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self._icon_label)

        self._text_label = QLabel(self)
        self._text_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        font = QFont(design.font_families().ui)
        size, weight = design.tokens.Font.TOAST
        font.setPixelSize(round(size))
        font.setWeight(QFont.Weight(weight))
        self._text_label.setFont(font)
        self._text_label.setStyleSheet(f"color: {design.color('TOAST_FG').name()};")
        layout.addWidget(self._text_label)

        # Single-shot, restarted (not re-created) by every show_message
        # call -- QTimer.start() on an already-running timer resets its
        # remaining time, which is exactly the spec's "restarts the timer"
        # rather than letting an earlier call's dismissal fire early.
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(metric.TOAST_MS)
        self._timer.timeout.connect(self.hide)

        self.hide()

    def show_message(self, icon_name: str, text: str, window_size: QSize) -> None:
        """Show `text` next to `icon_name`'s glyph, positioned at the
        bottom centre of `window_size`, and (re)start the `TOAST_MS`
        auto-dismiss timer.

        Updates this same widget's content rather than creating a new one
        -- there is only ever one toast on screen, per the class docstring
        -- so a second call while the first is still showing both replaces
        the message and restarts the timer in one step.
        """
        metric = design.tokens.Metric
        pixmap = design.icon(icon_name, design.color("TOAST_FG")).pixmap(
            self._ICON_SIZE, self._ICON_SIZE
        )
        self._icon_label.setPixmap(pixmap)
        self._text_label.setText(text)

        size = self.sizeHint()
        left = (window_size.width() - size.width()) / 2
        top = window_size.height() - metric.TOAST_BOTTOM - size.height()
        self.setGeometry(round(left), round(top), size.width(), size.height())

        self.show()
        self.raise_()  # "above everything" -- including the bar and trays
        self._timer.start()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        metric = design.tokens.Metric

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(design.color("TOAST_BG"))
        painter.drawRoundedRect(QRectF(self.rect()), metric.TOAST_RADIUS, metric.TOAST_RADIUS)
        painter.end()


# ---------------------------------------------------------------------------
# Delay countdown (SNX-50)
# ---------------------------------------------------------------------------
# docs/design/overlay-redesign.md's "Capture modes" entry for Delay is the
# authority: "the overlay dismisses, waits, re-grabs and re-opens. Show a
# countdown." The prototype never simulates Delay (Region is the only mode it
# simulates, per that same section's opening line), so there is no reference
# markup for the countdown's own look -- styled here from the same BAR_BG
# glass and TEXT_PRIMARY/mono readout the rest of this file's chrome already
# uses, rather than inventing a one-off palette for a single widget.


class DelayCountdown(QWidget):
    """A small, top-level countdown shown while `OverlayWindow` is hidden
    for a delayed re-capture (`OverlayWindow._start_delayed_capture`).

    Deliberately *not* a child of `OverlayWindow`, unlike every other piece
    of chrome in this file: the entire reason the overlay hides for the
    delay is so it isn't in its own screenshot (per the spec: "the overlay
    dismisses... so it is not in its own screenshot" is the point of Delay
    in the first place), and a child widget goes invisible the instant its
    parent does -- see `OverlayWindow.hideEvent`, which relies on exactly
    that to take `_bar`/`_tray`/`_popover`/`_toast`/`_hud` down with it. A
    countdown built the same way would vanish along with the window it is
    supposed to be standing in for, which is the one thing it must not do.
    """

    _SIZE = 96
    _BG_ALPHA = 0.72  # the same "glass over the desktop" treatment as the bar

    def __init__(self):
        # No parent, ever -- see the class docstring.
        super().__init__(None)
        # Frameless/always-on-top so it reads as a HUD rather than a window
        # a user could accidentally click into and lose focus of, mirroring
        # `Overlay`/`OverlayWindow`'s own `setWindowFlags` calls.
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedSize(self._SIZE, self._SIZE)

        self._label = QLabel(self)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setGeometry(0, 0, self._SIZE, self._SIZE)
        font = QFont(design.font_families().mono)
        font.setPixelSize(36)
        font.setWeight(QFont.Weight(600))
        self._label.setFont(font)
        self._label.setStyleSheet(f"color: {design.color('TEXT_PRIMARY').name()};")

    def set_seconds_remaining(self, seconds: int) -> None:
        self._label.setText(str(seconds))

    def show_centered_on(self, geometry: QRect) -> None:
        """Position centred over `geometry` -- the virtual-desktop rect the
        hidden `OverlayWindow` itself spans -- and show.
        """
        center = QRectF(geometry).center()
        self.move(round(center.x() - self._SIZE / 2), round(center.y() - self._SIZE / 2))
        self.show()
        self.raise_()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg = QColor(design.tokens.Color.BAR_BG)
        bg.setAlphaF(self._BG_ALPHA)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg)
        painter.drawEllipse(QRectF(self.rect()))
        painter.end()


# Tool name -> Shape subclass for a freehand (points-list) stroke, keyed
# by the same string ids tokens.TOOLS/FloatingBar use -- mirrors editor.py's
# _FREEHAND_SHAPE_CLASSES, just keyed by these strings instead of the old
# Tool enum, since OverlayWindow (unlike Canvas) never had one.
_FREEHAND_MARK_CLASSES = {"pen": Pen, "highlighter": Highlighter}

# Tool name -> Shape subclass for a press-to-release two-point stroke.
# 'blur' is deliberately absent: which of Blur/Pixelate it commits depends
# on `_blur_mode`, decided in `OverlayWindow._start_stroke` at press time
# rather than looked up here.
_TWO_POINT_MARK_CLASSES = {"arrow": Arrow, "rect": Rectangle}


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
    SNX-41 adds `SettingsTray` (`_tray`), shown and positioned under the
    bar by `_sync_tray_visibility` only while the bar's active tool is one
    of `tokens.DRAW_TOOLS` -- the eraser gets none -- and tracks the
    colour/stroke it emits as `_ink_colour`/`_stroke_width` for a later
    ticket's drawing tools to read. SNX-42 (this ticket) adds `BlurTray`
    (`_blur_tray`) alongside it: `_sync_tray_visibility` shows at most one
    of the two, `_blur_tray` while the active tool is `'blur'`, per the
    spec's "It replaces the colour and stroke tray rather than sitting
    alongside it" -- and tracks the toggle/slider it emits as
    `_blur_mode`/`_blur_strength`, the same way `_tray` does for colour and
    stroke. The drawing-tool mouse handling that would actually call
    `add_mark` from a live drag -- for every tool but the eraser, which
    SNX-38 already wired end to end -- is still a later ticket in the same
    arc; `_blur_mode` is what that ticket reads to decide which of
    shapes.py's `Blur`/`Pixelate` a blur drag commits. SNX-44 (this ticket)
    adds `CaptureModePopover` (`_popover`), opened by the bar's capture
    chip (`FloatingBar.captureChipClicked`) via `_toggle_capture_popover`
    and positioned by the popover's own `reposition` against `_bar`'s
    geometry. Picking a row tracks `_capture_mode` and updates the chip's
    own label to match, per the spec's "the chip is a mode selector, not
    an action" -- Window, Full screen and Freeform are separate tickets
    that will read `_capture_mode` once they exist, same as `_blur_mode`
    above sits unread until its own ticket. `mousePressEvent` closes the
    popover on any click that lands outside it, without touching
    `_capture_mode`. SNX-45 adds `Toast` (`_toast`), the single instance
    `_show_toast` shows for `copy`/`save`/`clear`/`discard`, gated on
    `self.isVisible()` the same way `_bar` already is, so it never paints
    into this window's own many pixel-sampling tests. SNX-46 adds `HintHUD`
    (`_hud`), gated on both `self.isVisible()` (same reason as `_bar`/
    `_toast`) and `_hints_enabled` -- the preference the spec's "Top hint
    HUD" section puts it behind, default on, toggled via
    `set_hints_enabled` -- via `_sync_hud_visibility`. SNX-47 (this ticket)
    adds `keyPressEvent`: tool letters from `tokens.SHORTCUTS`, Ctrl+Z /
    Ctrl+Shift+Z for undo/redo, Enter to copy-and-dismiss, and the
    two-stage Esc (`_handle_escape`) the spec leaves for us to decide --
    finally wiring `discard()` up to a key, per that method's own
    docstring. All of it is suppressed while a slider or a text-editing
    widget has focus (`_shortcuts_suppressed`). SNX-52 was
    what the "still a later ticket" note above `_on_tool_selected` used to
    point at: `mousePressEvent` now dispatches a press that misses every
    handle and lands inside the selection -- with the eraser disarmed --
    to `_start_stroke`, which arms `_in_progress_shape` for
    pen/highlighter/arrow/rect/blur (`mouseMoveEvent` extends it,
    `mouseReleaseEvent` runs it through `shapes.finalize_mark` and either
    commits or discards it), commits a `StepMarker` immediately for step,
    and opens the lazily-built `_text_edit` for text (`_start_text_entry`/
    `_commit_text`, mirroring editor.py's `Canvas._ensure_text_edit`). A
    committed mark always takes its colour/stroke from `_ink_colour`/
    `_stroke_width` and, for blur, its shape class and strength from
    `_blur_mode`/`_blur_strength` -- the same tray-tracked state prior
    tickets already left ready to be read. SNX-48 (this ticket) makes
    `_capture_mode` do something once it names Window or Full screen,
    instead of sitting unread the way `_on_capture_mode_selected`'s own
    docstring used to say it would: `_enter_window_mode` arms
    `_picking_window`, which `mouseMoveEvent`/`mousePressEvent` check
    ahead of the resize/stroke dispatch above so a hover previews the
    window under the cursor (`_geometry_provider.window_at`, the same
    `GeometryProvider` `Overlay` already takes) and a click snaps
    `_selection` to it (`_confirm_window_pick`); `_select_full_screen`
    sets `_selection` to whichever of `_monitor_geometries` contains the
    cursor's last known position immediately, no click needed past
    picking the row. Either way the result is handed to `set_selection`
    like any other -- nothing downstream distinguishes a mode-produced
    selection from a dragged one, which is what makes it re-framable and
    annotatable the same way. A `GeometryProvider` that reports itself
    unavailable (`UnsupportedGeometryProvider`, same as `Overlay`'s own
    default) must not leave Window mode looking chosen but inert: it
    toasts an explanation and falls back to Region instead, per the
    ticket's own acceptance criterion. SNX-49 (this ticket) makes Freeform
    do the same: `_enter_freeform_mode` arms `_picking_freeform`, which
    `mousePressEvent`/`mouseMoveEvent`/`mouseReleaseEvent` check ahead of
    the same resize/stroke dispatch, so a press starts tracing a lasso
    (`_start_freeform_drag`), motion extends it (`_extend_freeform_drag`),
    and release closes and confirms it (`_confirm_freeform_pick`) -- the
    same press-drag-release-close shape `Overlay`'s own FREEFORM handling
    above already uses, including its misfire threshold. The confirmed
    path is stored as `_selection_path`, alongside `_selection` (its own
    bounding rect, handed to `set_selection` like any other mode's result
    so the selection frame/handles/chips/bar stay exactly as re-framable
    and annotatable), and read by `_paint_scrim` (the dim scrim inverts
    against the path itself, not its bounding box) and `rendered_image`
    (via `shapes.render_selection`'s new `selection_path` parameter, which
    masks everything outside it transparent) -- per the spec's Freeform
    entry: "the selection becomes a path, the dim scrim inverts against
    it, and export crops to its bounding box with the outside transparent."
    SNX-50 (this ticket) makes `_delay` do something once it names anything
    but `Off`: `_on_capture_mode_selected` hands off to
    `_start_delayed_capture` instead of dispatching a mode immediately,
    which hides this whole window (so it isn't in its own screenshot),
    shows `DelayCountdown` (a separate, non-child top-level widget -- see
    its own docstring for why it can't be a child), and ticks a `QTimer`
    down once a second. `_finish_delayed_capture` then re-grabs through
    `_registry` -- the same `BackendRegistry` the first frame came through,
    injected the same way `geometry_provider` is -- and re-opens over the
    fresh frame by mutating `_frame` and re-showing in place, rather than
    building a second `OverlayWindow`: every other piece of state
    (`_ink_colour`, `_stroke_width`, `_bar`'s active tool, `_capture_mode`
    itself) already lives on `self` and needs no copying across. The mode
    picked when the delay was confirmed is remembered
    (`_pending_capture_mode`) and re-dispatched to the same
    Window/Full screen/Freeform handling above once the new frame is in,
    so a delayed Window/Full screen/Freeform pick still does its own thing
    on the new content instead of silently downgrading to Region. SNX-57
    (this ticket) gives Region itself -- the default mode, and the only one
    with no picking flag of its own -- the drag-to-create it was missing:
    `mousePressEvent` starts a `_region_drag_anchor` press when a press
    misses every handle and there is no selection yet, `mouseMoveEvent`
    tracks it live, and `mouseReleaseEvent` hands off to
    `_confirm_region_drag`, which commits it through `set_selection` like
    any other mode's result -- discarding it instead, per the minimum-size
    acceptance criterion, if it lands under `tokens.Metric.SEL_MIN_W/H`.

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

    # Chips above the selection (SNX-43), per docs/design/overlay-redesign.md's
    # "Chips above the selection" section: the reference gives these as
    # literal CSS values (`padding:6px 11px`, `border-radius:8px`, a 9px/7px
    # flex `gap`) rather than tokens.Metric entries -- same convention as the
    # corner bracket/edge handle constants above. tokens.Metric.CHIP_OFFSET_Y
    # is the one shared value that *is* tokenized, since both chips read it.
    _CHIP_RADIUS = 8
    _CHIP_PAD_V = 6
    _CHIP_PAD_H = 11
    _CHIP_INNER_GAP = 9    # dimension chip: gap between size / dot / mark count
    _CHIP_DOT = "·"   # the reference's bare "·" between size and mark count
    _FROZEN_INNER_GAP = 7  # frozen pill: gap between the pin icon and its label
    _FROZEN_ICON_SIZE = 13
    _FROZEN_LABEL = "Frozen"

    def __init__(
        self,
        frame: Frame,
        parent=None,
        hints_enabled: bool = True,
        geometry_provider: GeometryProvider | None = None,
        monitor_geometries: list[QRectF] | None = None,
        registry: BackendRegistry | None = None,
        on_dismissed: Callable[[], None] | None = None,
    ):
        super().__init__(parent)
        self._frame = frame
        # SNX-58: called once, from closeEvent, when this window is the
        # Wayland-primary of a multi-monitor `open_overlay` group -- the
        # hook that closes the non-interactive `_MonitorVeil` companions
        # covering the other monitors the moment this one does, so ending
        # the session on the primary never leaves a dimmed veil stuck on
        # another screen. None (the default, and the only value X11 or a
        # single-monitor session ever passes) means there is nothing to
        # close alongside this window.
        self._on_dismissed = on_dismissed

        # SNX-48: sourced for Window/Full screen capture-mode handling
        # below, mirroring `Overlay`'s own constructor args of the same
        # names. `_geometry_provider` defaults the same way `Overlay`'s
        # does -- `UnsupportedGeometryProvider` reports no windows
        # anywhere, which is what makes Window mode degrade instead of
        # needing a None-check at every call site. `_monitor_geometries`
        # defaults to the frame's own full span (there is no per-monitor
        # split to make without one) so a single-monitor caller -- every
        # test in this file included -- gets a correct Full screen
        # selection without having to pass one in.
        self._geometry_provider = geometry_provider or UnsupportedGeometryProvider()
        self._monitor_geometries = (
            list(monitor_geometries)
            if monitor_geometries
            else [QRectF(frame.logical_origin, frame.logical_size)]
        )

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
        # SNX-49: the exact traced lasso, window coordinates -- same space
        # as `_selection` -- only ever non-None right after a Freeform pick
        # confirms (`_confirm_freeform_pick`). `set_selection`'s own `path`
        # parameter defaults to clearing this, which is what keeps a stale
        # lasso from lingering once the selection changes by any other
        # means (a resize drag, Window/Full screen picks, a fresh Freeform
        # drag) -- see that method's own docstring.
        self._selection_path: QPainterPath | None = None

        # SNX-48: last-known pointer position over the frozen desktop
        # itself (window-local logical coords, the same space `_selection`
        # lives in) -- tracked from ordinary mouse-move events the same
        # way `Overlay._cursor_pos` is, rather than ever calling
        # `QCursor.pos()`, so `_select_full_screen` can answer "which
        # display is the cursor on" without this widget reaching for
        # global cursor state no test can control offscreen. None until
        # the first move.
        self._cursor_pos: QPointF | None = None
        # True from the moment Window mode is armed (`_enter_window_mode`)
        # until a click lands on a window (`_confirm_window_pick`), per
        # the spec's "hover highlights the window under the cursor...
        # click accepts it." While armed, `mousePressEvent`/
        # `mouseMoveEvent` dispatch to the Window-picking branch ahead of
        # the resize-handle/stroke logic below, the same "handled here,
        # nothing else runs" shape `_active_handle`/`_eraser_active`
        # already use for their own presses.
        self._picking_window = False

        # SNX-49: armed the same way `_picking_window` is, from the moment
        # Freeform mode is chosen (`_enter_freeform_mode`) until a full
        # press-drag-release lasso confirms (`_confirm_freeform_pick`) or
        # is discarded as too small. `_freeform_drag_path` is the lasso
        # currently being traced -- window coordinates, same space
        # `_selection`/`_marks` live in -- None outside an active drag.
        self._picking_freeform = False
        self._freeform_drag_path: QPainterPath | None = None

        # Handle currently being dragged (SNX-33 re-framing), and the
        # selection as it stood the moment that drag started. The anchor is
        # read-only for the drag's whole duration -- every edge it doesn't
        # own comes from here, never from the live selection -- which is
        # what keeps the opposite edge/corner from creeping as the mouse
        # moves. None outside a handle drag.
        self._active_handle: Handle | None = None
        self._resize_anchor: QRect | None = None

        # Region-mode drag-to-create (SNX-57): window-local logical anchor
        # of an in-progress left-button drag that is building a *brand new*
        # selection from nothing, as opposed to `_active_handle`'s resize of
        # an existing one. None outside such a drag. Only ever armed from
        # `mousePressEvent` when a press misses every handle and there is no
        # selection yet -- Window, Full screen and Freeform each already
        # produce their own first selection through `_confirm_window_pick`/
        # `_select_full_screen`/`_start_freeform_drag`, so this is what gives
        # Region -- the default mode, with no picking flag of its own --
        # the same "drag on an empty overlay" starting point the others get
        # for free.
        self._region_drag_anchor: QPointF | None = None

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

        # Live drawing (SNX-52): the mark a left-press/drag/release is
        # currently building, in this widget's own window coordinates --
        # the same space `_marks` lives in, per the class docstring. None
        # outside an active stroke; `mousePressEvent` never sets both this
        # and `_active_handle` for the same press, so a resize and a
        # stroke can never be in progress at once. See `_start_stroke`.
        self._in_progress_shape: Shape | None = None

        # The text tool (SNX-52): a lazily-built QLineEdit that mirrors
        # editor.py's `Canvas._ensure_text_edit`/`_commit_text` -- a click
        # opens it, seeded with a placeholder (never pre-filled text, so
        # committing with nothing typed still discards it, per
        # `_commit_text`'s own guard) and focused for immediate typing.
        # `_pending_text_*` holds the press-time colour/stroke/point until
        # `editingFinished` commits (or discards) them; `_committing_text`
        # guards against `hide()`'s own re-entrant `editingFinished`, same
        # as `Canvas._commit_text`'s own docstring explains.
        self._text_edit: QLineEdit | None = None
        self._pending_text_point: QPointF | None = None
        self._pending_text_colour: QColor | None = None
        self._pending_text_stroke_width: float | None = None
        self._committing_text = False

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
        self._bar.copyRequested.connect(self._on_bar_copy)
        self._bar.saveRequested.connect(self._on_bar_save)
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

        # The blur tray (SNX-42): `_tray`'s replacement, not its
        # companion, while the active tool is 'blur' -- see
        # `_sync_tray_visibility`, which shows at most one of the two.
        # `_blur_mode`/`_blur_strength` are tracked the same way
        # `_ink_colour`/`_stroke_width` are above -- unread by any drawing
        # code until the later ticket that wires a live blur drag to
        # actually call `add_mark` reads them to decide which of
        # shapes.py's Blur/Pixelate to commit.
        self._blur_mode: str = "blur"
        self._blur_strength: int = design.tokens.Metric.BLUR_DEFAULT
        self._blur_tray = BlurTray(self)
        self._blur_tray.hide()
        self._blur_tray.blurModeChanged.connect(self._on_blur_mode_changed)
        self._blur_tray.strengthChanged.connect(self._on_blur_strength_changed)

        # The capture-mode popover (SNX-44): opened from the bar's chip
        # click via `_toggle_capture_popover`, positioned by the popover's
        # own `reposition` against `_bar`'s geometry. `_capture_mode`/
        # `_delay` start at `tokens.CAPTURE_MODES`/`tokens.DELAYS`' own
        # first entries -- the same "unread until a later ticket" state
        # `_ink_colour`/`_blur_mode` above already are, except for the chip
        # label itself, which `_on_capture_mode_selected` keeps in sync.
        self._capture_mode: str = design.tokens.CAPTURE_MODES[0][0]
        self._delay: str = design.tokens.DELAYS[0]
        self._popover = CaptureModePopover(self)
        self._popover.hide()
        self._popover.modeSelected.connect(self._on_capture_mode_selected)
        self._popover.delayChanged.connect(self._on_delay_changed)
        self._bar.captureChipClicked.connect(self._toggle_capture_popover)

        # The delayed re-capture (SNX-50): `_registry` is what
        # `_finish_delayed_capture` re-grabs through -- an empty
        # `BackendRegistry` by default, the same "an inert default degrades
        # instead of needing a None-check everywhere" shape
        # `UnsupportedGeometryProvider` already gives `_geometry_provider`
        # above (an empty registry's own `capture()` raises `CaptureError`
        # with its own "no capture backend is available" message, which
        # `_finish_delayed_capture` already has to handle for a real,
        # non-empty registry that simply fails). `_countdown`/`_delay_timer`
        # are built lazily, on the first delay actually confirmed, rather
        # than unconditionally here -- unlike `_toast`/`_hud`, `_countdown`
        # is not a child of this window (see its own docstring for why) and
        # every `OverlayWindow` this file's tests build would otherwise
        # leave a real, if hidden, extra top-level widget behind it.
        # `_pending_capture_mode` is the mode `_on_capture_mode_selected`
        # was confirming when the delay started, re-dispatched once the
        # fresh frame is in.
        self._registry = registry if registry is not None else BackendRegistry()
        self._countdown: DelayCountdown | None = None
        self._delay_timer: QTimer | None = None
        self._delay_remaining = 0
        self._pending_capture_mode: str | None = None

        # The toast (SNX-45): the single `Toast` instance `copy`/`save`/
        # `clear`/`discard` below all share -- see `_show_toast` for the
        # `self.isVisible()` gate that keeps it from painting into this
        # window's own many pixel-sampling tests, none of which call
        # `.show()`, the same reason `_sync_bar_visibility` gates `_bar`.
        self._toast = Toast(self)

        # The top hint HUD (SNX-46): behind the `hints` preference the spec
        # puts it behind, default on. A real child widget the same way
        # `_bar`/`_toast` are -- see `_sync_hud_visibility` for the same
        # `self.isVisible()` gate those two already use, here paired with
        # `_hints_enabled` so turning the preference off hides the bar
        # immediately regardless of this window's own visibility. Spans the
        # window's full width at construction time -- this window's own
        # geometry is set once above and never resized afterwards (a
        # fullscreen overlay), so there is no resizeEvent to keep this in
        # sync with.
        self._hints_enabled = hints_enabled
        self._hud = HintHUD(self)
        self._hud.setGeometry(0, 0, self.width(), design.tokens.Metric.HUD_H)
        self._hud.hide()

    def set_selection(self, rect: QRect | None, path: QPainterPath | None = None) -> None:
        """Set the current selection (window coordinates) and repaint.

        `path` (SNX-49) is only ever passed by `_confirm_freeform_pick`,
        with the exact lasso `_selection`'s bounding box was taken from --
        every other caller (a resize drag, Window/Full screen picks, a
        fresh Freeform drag's own `set_selection(None)`) leaves it at the
        default `None`, which is what keeps a previously-confirmed lasso
        from lingering once the selection changes by some other means. In
        particular, re-framing a Freeform selection via its resize handles
        silently reverts it to a plain rectangle: the path was traced
        against the *original* bounding box, and `_resize_selection` has no
        way to reshape it to match a dragged edge, so keeping a now-stale
        path around would be worse than dropping it.
        """
        self._selection = rect
        self._selection_path = path
        self._sync_bar_visibility()
        self.update()

    def _on_tool_selected(self, tool: str) -> None:
        """Wire the bar's tool buttons to the eraser's hit-testing
        arm/disarm (see `set_eraser_active`) and the settings tray's
        visibility. `mousePressEvent`/`_start_stroke` read `self._bar.
        active_tool` directly at press time rather than this class keeping
        a second copy of it -- `FloatingBar` is already the one place a
        click and a shortcut key (`keyPressEvent`'s tool letters) both
        funnel through (`FloatingBar.select_tool`'s own docstring), so
        there is nothing for this method to track beyond the two things
        below that don't already live on the bar.
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

    def _on_blur_mode_changed(self, mode: str) -> None:
        """Track the blur tray's active segment -- 'blur' or 'pix' -- for
        the still-later ticket that wires a live blur drag to read it when
        deciding which of shapes.py's Blur/Pixelate to commit.
        """
        self._blur_mode = mode

    def _on_blur_strength_changed(self, strength: int) -> None:
        self._blur_strength = strength

    # -- capture-mode popover (SNX-44) --------------------------------------

    def _toggle_capture_popover(self) -> None:
        """Open/close the capture-mode popover from the bar's chip click,
        per the spec's "The chip is a mode selector... Opens the popover."

        A second click while it's already open closes it again, mirroring
        the reference's own `toggleMode: () => this.setState({ modeOpen:
        !st.modeOpen })` -- there is no other way to close it from the chip
        itself once it's open.
        """
        if self._popover.isVisible():
            self._popover.hide()
            return
        self._popover.reposition(self._bar.geometry(), self.size())
        self._popover.show()
        self._popover.raise_()

    def _on_capture_mode_selected(self, mode: str) -> None:
        """Track the popover's chosen capture mode and update the chip's
        own label to match, per the reference's `{{ mode }}` binding on
        the chip button itself.

        SNX-48 makes Window and Full screen actually do something past
        the label update: `_enter_window_mode` arms hover-preview/click-
        to-snap picking, `_select_full_screen` snaps `_selection`
        immediately. SNX-49 does the same for Freeform:
        `_enter_freeform_mode` arms press-drag-release lasso tracing.
        Whatever picking was in progress for a previous mode is disarmed
        unconditionally first -- switching away from Window or Freeform
        mid-pick must not leave `_picking_window`/`_picking_freeform`
        stuck armed underneath whatever the newly-picked mode does
        instead.

        SNX-50 (this ticket) intercepts all of the above whenever `_delay`
        isn't `Off`: per the spec's Delay entry, confirming a mode while a
        delay is set dismisses the overlay, waits, re-grabs and re-opens,
        rather than acting on the current (soon to be stale) frame right
        away. `_start_delayed_capture` takes over from here and
        re-dispatches to this same mode logic itself, once the fresh frame
        is in.
        """
        self._picking_window = False
        self._picking_freeform = False
        self._freeform_drag_path = None
        # SNX-57: a mode switch mid-drag must not leave this armed under
        # whatever the newly-picked mode does instead, same reasoning as
        # `_freeform_drag_path` above.
        self._region_drag_anchor = None
        self._capture_mode = mode
        self._bar.set_capture_mode(mode)

        if self._delay != "Off":  # design.tokens.DELAYS[0]
            self._start_delayed_capture(mode)
            return

        self._dispatch_capture_mode(mode)

    def _dispatch_capture_mode(self, mode: str) -> None:
        """Run whichever mode-specific picking `mode` itself calls for --
        the immediate half of `_on_capture_mode_selected`, factored out so
        `_finish_delayed_capture` can re-run it against a fresh frame
        without also re-running the delay check above (which would just
        recurse into another wait).
        """
        if mode == "Window":  # design.tokens.CAPTURE_MODES[1][0]
            self._enter_window_mode()
        elif mode == "Full screen":  # design.tokens.CAPTURE_MODES[2][0]
            self._select_full_screen()
        elif mode == "Freeform":  # design.tokens.CAPTURE_MODES[3][0]
            self._enter_freeform_mode()

    # -- delayed re-capture (SNX-50) -----------------------------------------
    # docs/design/overlay-redesign.md's "Capture modes" entry for Delay is
    # the authority: "Off / 3s / 5s / 10s. When set, the overlay dismisses,
    # waits, re-grabs and re-opens. Show a countdown." The re-grab is a
    # second, independent call through `_registry` -- the same
    # `BackendRegistry` the first frame came through, per CLAUDE.md's one
    # architectural rule applying to this grab exactly as it does to the
    # first -- never a re-use of `_frame` as it stood before the wait.

    def _start_delayed_capture(self, mode: str) -> None:
        """Hide this window and start counting the seconds in `_delay`
        down, per the spec's "the overlay dismisses, waits" -- hiding
        happens synchronously, before the wait itself begins, so the
        overlay is never on screen (and never in danger of being in its
        own next screenshot) for any part of the delay.

        `mode` is remembered as `_pending_capture_mode` and re-dispatched
        by `_finish_delayed_capture` once the fresh frame is in, so a
        delayed Window/Full screen/Freeform pick still does its own thing
        against the new content instead of only ever landing on Region.
        """
        self._pending_capture_mode = mode
        self._delay_remaining = int(self._delay.rstrip("s"))
        self.hide()

        if self._countdown is None:
            self._countdown = DelayCountdown()
        self._countdown.set_seconds_remaining(self._delay_remaining)
        # `self.geometry()` is still the real virtual-desktop rect this
        # window spans -- hiding a QWidget doesn't clear its geometry --
        # so the countdown can centre on it without this window needing to
        # stay visible to answer the question.
        self._countdown.show_centered_on(self.geometry())

        if self._delay_timer is None:
            self._delay_timer = QTimer(self)
            self._delay_timer.setInterval(1000)
            self._delay_timer.timeout.connect(self._tick_delay)
        self._delay_timer.start()

    def _tick_delay(self) -> None:
        """One second of the countdown elapsing. Reaching zero stops the
        timer and hands off to `_finish_delayed_capture` -- this method
        itself never touches `_registry` or `_frame`.
        """
        self._delay_remaining -= 1
        if self._delay_remaining <= 0:
            self._delay_timer.stop()
            self._countdown.hide()
            self._finish_delayed_capture()
            return
        self._countdown.set_seconds_remaining(self._delay_remaining)

    def _finish_delayed_capture(self) -> None:
        """Re-grab through `_registry` and re-open over the fresh frame.

        A failed re-grab (per CLAUDE.md, a capture failure must not take
        down the rest of the app) leaves the *old* frame in place and
        simply re-shows this window with it, toasted, rather than leaving
        the user with no overlay and no explanation at all -- mirrors
        `_enter_window_mode`'s own "toast and fall back" handling of an
        unavailable `GeometryProvider`.

        On success, `_frame` and this window's own geometry are replaced
        in place (this is still the same `OverlayWindow`, never a second
        one) -- which is what leaves `_ink_colour`/`_stroke_width`/the
        bar's active tool/`_capture_mode` itself exactly as the user had
        them, with nothing to copy across, per the ticket's "the overlay
        re-opens... with the tool, colour and stroke settings the user had
        chosen" acceptance criterion. The stale selection is cleared --
        it described a rectangle of the *old* content -- and the picked
        mode is re-dispatched against the new one.
        """
        mode = self._pending_capture_mode
        try:
            frame = self._registry.capture()
        except CaptureError as exc:
            self.show()
            self._show_toast("timer", str(exc))
            return

        self._frame = frame
        self.setGeometry(
            round(frame.logical_origin.x()),
            round(frame.logical_origin.y()),
            round(frame.logical_size.width()),
            round(frame.logical_size.height()),
        )
        self._hud.setGeometry(0, 0, self.width(), design.tokens.Metric.HUD_H)
        self.set_selection(None)
        self.show()
        self._dispatch_capture_mode(mode)

    # -- Window / Full screen capture modes (SNX-48) -------------------------
    # docs/design/overlay-redesign.md's "Capture modes" section is the
    # authority: "Window -- hover highlights the window under the cursor
    # (snap the selection to its frame); click accepts it. Then annotation
    # proceeds identically" and "Full screen -- selection = the whole
    # display." Both mirror `Overlay`'s own WINDOW/FULL_SCREEN handling
    # above, but hand their result to this window's own `set_selection`
    # instead of emitting `confirmed` into a separate editor -- the whole
    # point of this ticket is that the result stays open for re-framing
    # and in-place annotation exactly like a dragged selection.

    def _enter_window_mode(self) -> None:
        """Arm Window-mode picking, or -- per the ticket's acceptance
        criterion -- tell the user and fall back to Region if this
        platform has no `GeometryProvider` that can answer at all.

        A silent no-op here (the trap `UnsupportedGeometryProvider`'s own
        docstring warns "isn't load-bearing for the mode-3 fallback
        itself" against) would leave the chip reading "Window" for a mode
        that can never produce anything -- indistinguishable from a bug.
        Instead this toasts an explanation and reverts `_capture_mode`,
        the chip label, and the popover's own selected row back to
        Region, mirroring `CaptureModePopover.set_mode`'s own "seeding
        the popover from elsewhere" use case.
        """
        if not self._geometry_provider.is_available():
            self._show_toast("window", "Window capture isn't available on this session")
            fallback = design.tokens.CAPTURE_MODES[0][0]  # "Region"
            self._capture_mode = fallback
            self._bar.set_capture_mode(fallback)
            self._popover.set_mode(fallback)
            return
        self._picking_window = True
        # Whatever was selected before (if anything) is not a Window-mode
        # preview and must not linger on screen while the user hasn't
        # hovered a window yet -- mirrors `Overlay`'s own hover branch,
        # which likewise clears on a miss rather than leaving a stale rect.
        self.set_selection(None)

    def _confirm_window_pick(self, pos: QPointF) -> None:
        """Snap `_selection` to the window under `pos` (this widget's own
        window-local coordinates) and disarm picking. Only ever reached
        from `mousePressEvent` while `_picking_window` is armed.

        A miss leaves `_picking_window` armed and `_selection` as the
        last hover left it (already `None`, per `mouseMoveEvent`'s own
        miss handling below) -- the user can simply move and click again,
        rather than one mis-click ending the mode for good.
        """
        rect = self._geometry_provider.window_at(self._to_absolute(pos))
        if rect is None:
            return
        self._picking_window = False
        self.set_selection(self._to_local_rect(rect).toRect())

    def _select_full_screen(self) -> None:
        """Set `_selection` to the whole display the cursor is on, per
        the spec's "Full screen -- selection = the whole display" and
        this ticket's cursor-aware acceptance criterion. Snaps
        immediately -- no drag, no click needed past picking the row.

        Falls back to this window's own centre when the cursor has never
        moved over the frozen desktop yet (`_cursor_pos` is still
        `None`) -- a real overlay is always shown full-screen under the
        pointer, so this only matters for a caller (a test, or the very
        first popover interaction) that never issued a prior move.
        """
        cursor = (
            self._cursor_pos
            if self._cursor_pos is not None
            else QPointF(self.width() / 2, self.height() / 2)
        )
        rect = self._monitor_at(self._to_absolute(cursor))
        self.set_selection(self._to_local_rect(rect).toRect())

    # -- Freeform capture mode (SNX-49) --------------------------------------
    # docs/design/overlay-redesign.md's "Capture modes" entry for Freeform is
    # the authority: "lasso; the selection becomes a path, the dim scrim
    # inverts against it, and export crops to its bounding box with the
    # outside transparent." Unlike Window/Full screen above, this mode's
    # selection comes from an ordinary press-drag-release -- the same
    # gesture `Overlay.mousePressEvent`/`mouseMoveEvent`/`mouseReleaseEvent`
    # already use for their own FREEFORM handling above, adapted to this
    # window's own coordinate space and `set_selection`'s `path` parameter
    # rather than a `confirmed` signal.

    def _enter_freeform_mode(self) -> None:
        """Arm Freeform-mode lasso tracing. Mirrors `_enter_window_mode`'s
        own "nothing selected yet" clear: whatever was selected before must
        not linger on screen while the user hasn't started tracing a new
        lasso.
        """
        self._picking_freeform = True
        self.set_selection(None)

    def _start_freeform_drag(self, pos: QPointF) -> None:
        """Begin tracing a lasso at `pos` (window coordinates). Only ever
        reached from `mousePressEvent` while `_picking_freeform` is armed.
        """
        self._freeform_drag_path = QPainterPath()
        self._freeform_drag_path.moveTo(pos)
        # Something to show from the very first pixel, mirroring `Overlay`'s
        # own FREEFORM press handling above.
        self.set_selection(QRect(pos.toPoint(), QSize(0, 0)))

    def _extend_freeform_drag(self, pos: QPointF) -> None:
        self._freeform_drag_path.lineTo(pos)
        self.set_selection(self._freeform_drag_path.boundingRect().toRect())

    def _confirm_freeform_pick(self, pos: QPointF) -> None:
        """End the lasso at `pos` and confirm it, or discard it as a
        misfire. Only ever reached from `mouseReleaseEvent` while
        `_picking_freeform` is armed and a drag is in progress.

        The release itself is traced as a point, same as every intermediate
        move -- omitting it would silently drop the final drag segment and
        let `closeSubpath()` cut straight from the last *moved-to* point
        back to the anchor instead, per the ticket's "a lasso that is not
        closed by the user is closed for them on release" -- `closeSubpath()`
        is exactly that closing, unconditional regardless of where the
        release landed relative to the anchor. Mirrors `Overlay`'s own
        freeform release handling, including its misfire threshold: measured
        by the traced path's own bounding-rect diagonal, not anchor-to-
        release distance, so a closed loop back near its start point isn't
        misfired away just because its last pixel lands next to its first.
        """
        path = self._freeform_drag_path
        self._freeform_drag_path = None
        path.lineTo(pos)
        path.closeSubpath()
        bounds = path.boundingRect().toRect()
        diagonal = math.hypot(bounds.width(), bounds.height())
        if diagonal < QApplication.startDragDistance():
            self.set_selection(None)
            return
        self._picking_freeform = False
        self.set_selection(bounds, path=path)

    def _monitor_at(self, absolute_point: QPointF) -> QRectF:
        """The `_monitor_geometries` entry containing `absolute_point`
        (absolute logical virtual-desktop coordinates), or the frame's
        own full span if none does -- a point can land outside every
        known monitor only when `_monitor_geometries` wasn't supplied
        accurately, and the whole capture is the only sane rect left to
        offer rather than raising.
        """
        for geometry in self._monitor_geometries:
            if geometry.contains(absolute_point):
                return geometry
        return QRectF(self._frame.logical_origin, self._frame.logical_size)

    def _to_absolute(self, local_point: QPointF) -> QPointF:
        """This widget's own window-local logical point -> absolute
        logical virtual-desktop point -- the space `GeometryProvider`/
        `_monitor_geometries` both use, the same conversion `Overlay.
        _to_absolute` performs for its own (differently-anchored) local
        space.
        """
        return local_point + self._frame.logical_origin

    def _to_local_rect(self, absolute_rect: QRectF) -> QRectF:
        """Absolute logical virtual-desktop rect -> this widget's own
        window-local logical rect -- the inverse of `_to_absolute`,
        applied to a rect rather than a point for `_confirm_window_pick`/
        `_select_full_screen`, whose `GeometryProvider`/
        `_monitor_geometries` results both arrive in absolute coordinates.
        """
        return QRectF(absolute_rect.topLeft() - self._frame.logical_origin, absolute_rect.size())

    def _on_delay_changed(self, delay: str) -> None:
        self._delay = delay

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
            self._blur_tray.hide()
            self._popover.hide()

    def _sync_tray_visibility(self) -> None:
        """Show/hide and reposition whichever settings tray -- draw or
        blur -- matches the bar's active tool, keeping the other hidden.

        At most one of `_tray`/`_blur_tray` is ever visible at once, per
        the spec's "It replaces the colour and stroke tray rather than
        sitting alongside it" -- neither colour nor stroke means anything
        to an obscuring shape, so blur gets `_blur_tray` in `_tray`'s place
        rather than a row added to it.

        Gated on the bar's own visibility rather than re-checking
        `_selection`/`self.isVisible()` directly -- the bar is already the
        single source of truth for "is this window's chrome allowed to be
        on screen right now," and the trays sit directly below it, so
        piggybacking on that check is what keeps the two from being able to
        disagree.
        """
        tool = self._bar.active_tool
        if not self._bar.isVisible():
            self._tray.hide()
            self._blur_tray.hide()
            return

        if tool in design.tokens.DRAW_TOOLS:
            self._blur_tray.hide()
            self._tray.set_tool(tool)
            self._reposition_tray(self._tray)
        elif tool == "blur":
            self._tray.hide()
            self._blur_tray.show()
            self._reposition_tray(self._blur_tray)
        else:
            self._tray.hide()
            self._blur_tray.hide()

    def _reposition_tray(self, tray: QWidget) -> None:
        """Centre `tray` under the bar, `TRAY_OFFSET_Y` below it -- per the
        spec's "Sits 8px below the bar, centred on it." Shared by `_tray`
        and `_blur_tray`, which `_sync_tray_visibility` never shows at the
        same time.
        """
        metric = design.tokens.Metric
        bar_geometry = self._bar.geometry()
        size = tray.sizeHint()
        center_x = bar_geometry.center().x()
        top = bar_geometry.bottom() + metric.TRAY_OFFSET_Y
        tray.setGeometry(
            round(center_x - size.width() / 2), round(top), size.width(), size.height()
        )

    def _sync_bar_undo_redo(self) -> None:
        self._bar.set_undo_enabled(self.can_undo)
        self._bar.set_redo_enabled(self.can_redo)

    # -- toast (SNX-45) ------------------------------------------------------

    def _show_toast(self, icon_name: str, text: str) -> None:
        """Show `_toast` for `icon_name`/`text`, gated on `self.isVisible()`.

        Mirrors `_sync_bar_visibility`'s own guard on `_bar`: a child
        widget's `.show()` call is enough to make Qt paint it into a
        `grab()` of this window regardless of whether this window itself
        was ever shown, and none of this file's many other pixel-sampling
        tests call `.show()` first -- so a toast triggered by copy()/
        save()/clear()/discard() must stay off screen until this window
        actually is, same as the bar/tray/popover already do.
        """
        if self.isVisible():
            self._toast.show_message(icon_name, text, self.size())

    # -- top hint HUD (SNX-46) -----------------------------------------------

    @property
    def hints_enabled(self) -> bool:
        return self._hints_enabled

    def set_hints_enabled(self, enabled: bool) -> None:
        """Toggle the top hint HUD -- the preference docs/design/overlay-
        redesign.md's "Top hint HUD" section puts it behind (`hints`,
        default on). Turning it off hides `_hud` immediately; turning it
        back on shows it again as soon as `_sync_hud_visibility`'s other
        gate -- `self.isVisible()` -- is also true, same as flipping
        `_selection` does for `_bar`.
        """
        self._hints_enabled = enabled
        self._sync_hud_visibility()

    def _sync_hud_visibility(self) -> None:
        """Show/hide `_hud` to match `_hints_enabled` and this window's own
        visibility.

        Gated on `self.isVisible()` for the same reason `_sync_bar_
        visibility`/`_show_toast` are: a child widget's own `.show()` call
        is enough to make Qt paint it into a `grab()` of this window
        regardless of whether this window itself was ever shown, and this
        file's many pixel-sampling tests never call `.show()` first -- so
        the HUD must stay off screen until this window actually is, same as
        the bar/tray/toast already do, on top of respecting the preference.
        """
        if self._hints_enabled and self.isVisible():
            self._hud.show()
            self._hud.raise_()
        else:
            self._hud.hide()

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
        """Discard every mark and both stacks in a single step, and toast
        `Ink cleared`.

        Per the spec: "Clear-ink empties both and toasts" -- and clearing
        is explicitly *not* itself undoable, unlike an ordinary undo/redo
        entry: the cleared marks are dropped outright rather than pushed
        onto `_redo`, so a subsequent undo() has nothing left to pop and
        cannot bring them back.
        """
        self._empty_marks()
        self._show_toast("trash", "Ink cleared")

    def discard(self) -> None:
        """Discard every mark and both stacks, and toast `Ink discarded`.

        Same underlying effect as `clear()` -- per the spec's keyboard
        table, "Esc -- discard all ink, toast Ink discarded" -- but its own
        method and toast message, since Esc's wording is deliberately
        distinct from the floating bar's own clear-ink button. Called by
        `_handle_escape` (SNX-47) as the first stage of Esc's two-stage
        behaviour, whenever there is ink present to discard.
        """
        self._empty_marks()
        self._show_toast("trash", "Ink discarded")

    def _empty_marks(self) -> None:
        """The shared body of `clear()`/`discard()`: empty `_marks` and
        `_redo` and resync the bar's undo/redo buttons, without deciding
        which toast (if any) to show -- that choice is each caller's own.
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

        `_selection_path`, set only for a just-confirmed Freeform lasso,
        is passed straight through: `render_selection` is what actually
        masks the pixels outside it transparent, per the "Capture modes"
        entry for Freeform.
        """
        if self._selection is None:
            raise ValueError("no selection to export")
        return render_selection(
            self._frame, self._marks, QRectF(self._selection), self._selection_path
        )

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
        """Flatten the marks present *right now* onto the selection's crop,
        place the result on the clipboard, and toast `Copied to clipboard`.
        """
        from snipux.app import copy_image_to_clipboard

        copy_image_to_clipboard(self.rendered_image())
        self._show_toast("copy", "Copied to clipboard")

    # Subdirectory of ~/Pictures saves land in -- per the spec's "Save
    # writes a timestamped PNG to ~/Pictures/snipux, creating the
    # directory." `app.save_image`'s own default (bare ~/Pictures, used by
    # editor.py's still-existing Editor) doesn't know about this
    # subdirectory, so it's supplied here rather than changed there.
    SAVE_SUBDIRECTORY = "snipux"

    def save(self) -> Path:
        """Flatten the marks present *right now* onto the selection's crop
        and write it as a timestamped PNG under ~/Pictures/snipux, creating
        that directory if it doesn't exist yet. Returns the path written,
        and toasts `Saved to ~/Pictures/snipux`.
        """
        from snipux.app import save_image

        directory = Path.home() / "Pictures" / self.SAVE_SUBDIRECTORY
        path = save_image(self.rendered_image(), directory)
        self._show_toast("save", f"Saved to ~/Pictures/{self.SAVE_SUBDIRECTORY}")
        return path

    # SNX-62: the bar's Copy/Save buttons, unlike `copy()`/`save()`
    # themselves, must also end the snip -- taking a snip should end the
    # snip, the same as Enter's own copy-and-dismiss below already does.
    # Wired to these wrapper methods rather than closing inside `copy()`/
    # `save()` directly: those two stay pure flatten-and-emit actions,
    # callable (and tested) without a close side effect, e.g. from Enter's
    # own handler, which already pairs its own `self.close()` explicitly,
    # or from a test asserting on `_toast` after the call -- `hideEvent`
    # hides `_toast` along with everything else, so a `close()` buried
    # inside `copy()`/`save()` would make that toast invisible again before
    # a caller ever got to look at it.

    def _on_bar_copy(self) -> None:
        """The floating bar's Copy button: copy, then dismiss."""
        self.copy()
        self.close()

    def _on_bar_save(self) -> None:
        """The floating bar's Save button: save, then dismiss."""
        self.save()
        self.close()

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
        # Likewise the HUD: `_hints_enabled` defaults on and may already be
        # true before this window was ever shown.
        self._sync_hud_visibility()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._ants_timer.stop()
        self._bar.hide()
        self._tray.hide()
        self._popover.hide()
        self._toast.hide()
        self._hud.hide()

    def closeEvent(self, event) -> None:
        # Deliberately not hideEvent: `_start_delayed_capture` (SNX-50)
        # also plain-hides this same window mid-countdown and re-shows it
        # in place a moment later, which must not tear down this window's
        # own `_MonitorVeil` companions (if any) -- only an actual close()
        # (today, only the second stage of Esc) means the session itself
        # is over.
        super().closeEvent(event)
        if self._on_dismissed is not None:
            self._on_dismissed()

    def show_on_screen(self, screen: QScreen | None) -> None:
        """Show this window, positioned for whichever session type the
        caller (`open_overlay`) already detected -- never assumed here.

        `screen` is None for X11 (and for any caller with no real `QScreen`
        to hand, e.g. the offscreen platform tests run under): plain
        `show()`, which lands this window at the geometry `__init__`
        already set via `setGeometry(frame.logical_origin, ...)` -- X11
        honours a client's requested position, so that alone is correct
        and unchanged from before this ticket.

        `screen` given means Wayland: a client there cannot choose its own
        window's position at all (SNX-58) -- the compositor decides, and
        a plain shown window routinely lands away from the real pixels the
        frame was captured from, which is what read as the desktop
        appearing twice, shifted. Fullscreen is the one state whose
        placement *is* the compositor's job, guaranteed to match `screen`
        exactly, so this requests that instead of a plain shown geometry.
        `winId()` forces the native window to exist first -- `windowHandle()`
        is None until it does -- so `setScreen` has something to act on.
        """
        if screen is None:
            self.show()
        else:
            self.winId()
            handle = self.windowHandle()
            if handle is not None:
                handle.setScreen(screen)
            self.showFullScreen()
        # Above every other window and focused the moment it opens, per
        # the acceptance criterion -- WindowStaysOnTopHint alone (set in
        # __init__) keeps it on top but doesn't itself force keyboard
        # focus, particularly right after a fullscreen state change.
        self.raise_()
        self.activateWindow()

    # -- keyboard shortcuts (SNX-47) -----------------------------------------
    # docs/design/overlay-redesign.md's "Keyboard" table is the authority: a
    # tool letter from tokens.SHORTCUTS, Ctrl+Z / Ctrl+Shift+Z for undo/redo,
    # Enter to copy-and-dismiss, and Esc, whose second stage the table
    # explicitly leaves for us to decide -- see _handle_escape. All of it is
    # suppressed outright while a text label or a slider has focus, per the
    # table's own closing line -- see _shortcuts_suppressed.

    def _shortcuts_suppressed(self) -> bool:
        """True while keyboard focus is on a widget these shortcuts must
        leave alone: a slider (either tray's stroke/strength control) or a
        text-editing widget -- `QLineEdit` is what the text tool's own label
        editor will be, per `shapes.Text`'s docstring, mirroring editor.py's
        `Canvas._ensure_text_edit`; checking for it now means this method
        doesn't have to change again once a later ticket wires the text
        tool's drag up to actually create one.

        `self.focusWidget()`, not the process-wide `QApplication.
        focusWidget()`, is enough here: every widget these shortcuts must
        yield to lives inside this window, and `QWidget.focusWidget()`
        reports a child that's been given focus via `setFocus()` regardless
        of whether this window itself is ever shown -- which is what lets a
        test give a tray's slider focus without a real, visible window.
        """
        focus = self.focusWidget()
        return isinstance(focus, (QSlider, QLineEdit))

    def _handle_escape(self) -> None:
        """Two-stage Esc -- the decision the spec leaves to us: "in the real
        app decide whether Esc should also dismiss the overlay, and if so
        make it two-stage (ink first, then close)." While any ink is
        present, the first press only discards it (`discard()`, which
        toasts "Ink discarded") and leaves the overlay open so re-framing
        can continue; once there is nothing left to discard, the next press
        closes the overlay without capturing -- `Overlay`'s own Escape
        above is unconditional cancel because it has no ink to lose first.
        """
        if self._marks:
            self.discard()
        else:
            self.close()

    def keyPressEvent(self, event) -> None:
        if self._shortcuts_suppressed():
            super().keyPressEvent(event)
            return

        key = event.key()
        modifiers = event.modifiers()

        # Exact-equality modifier check, not bitwise: Ctrl+Shift+Z's
        # modifier set includes ControlModifier, so a bitwise "is Control
        # held" test would swallow every redo as an undo. Same trap
        # editor.py's own `_undo_redo_action` docstring documents.
        if key == Qt.Key.Key_Z and modifiers == (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
        ):
            self.redo()
            return
        if key == Qt.Key.Key_Z and modifiers == Qt.KeyboardModifier.ControlModifier:
            self.undo()
            return

        if key == Qt.Key.Key_Escape:
            self._handle_escape()
            return

        if key in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
            # Mirrors Overlay's own Enter handling above: nothing to copy
            # (and dismiss) without a selection yet.
            if self._selection is not None:
                self.copy()
                self.close()
            return

        tool = _SHORTCUT_KEY_CODES.get(key)
        if tool is not None:
            self._bar.select_tool(tool)
            return

        super().keyPressEvent(event)

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
        if self._popover.isVisible() and not self._popover.geometry().contains(
            event.position().toPoint()
        ):
            # Per the spec's "clicking outside the popover closes it
            # without changing the mode": the click is consumed by the
            # dismissal alone and returns here, rather than falling through
            # to the handle/eraser logic below, so it can't also start a
            # resize or an erase underneath the popover in the same press.
            self._popover.hide()
            return
        if self._picking_window:
            # A press while armed is always a pick, never a resize or a
            # stroke -- returns unconditionally, the same "stop event
            # propagation" rule the handle branch below already follows.
            self._confirm_window_pick(event.position())
            return
        if self._picking_freeform:
            # Same "stop event propagation" rule as Window mode above: a
            # press while armed always (re)starts a lasso, never a resize
            # or a stroke.
            self._start_freeform_drag(event.position())
            return
        handle = self._handle_at(event.position())
        if handle is None:
            if self._selection is not None and QRectF(self._selection).contains(
                event.position()
            ):
                if self._eraser_active:
                    # No drag, per the spec's "Drawing": "eraser -- no
                    # drag." A miss (nothing under the cursor) is already a
                    # safe no-op inside erase_at itself.
                    self.erase_at(event.position())
                else:
                    self._start_stroke(event.position())
            elif self._selection is None:
                # SNX-57: Region -- the default mode, armed by nothing above
                # -- gets no selection at all otherwise: Window/Full screen/
                # Freeform each set one before a plain press could ever
                # reach here. A press on the empty overlay starts an
                # ordinary rectangle drag, the same press-drag-release shape
                # `Overlay`'s own RECTANGLE mode already uses.
                self._region_drag_anchor = event.position()
                self.set_selection(QRect(event.position().toPoint(), QSize(0, 0)))
            # A press outside an *existing* selection is a no-op for every
            # tool. Falling through to this no-op -- rather than the handle
            # branch below -- is exactly what "stop event propagation at the
            # handle" needs from this method: a stroke only ever starts when
            # a handle wasn't hit.
            return
        # Per the spec: a handle press is a resize, never a stroke, and
        # returning here means nothing past this point runs for it.
        self._active_handle = handle
        self._resize_anchor = QRect(self._selection)

    def _start_stroke(self, pos: QPointF) -> None:
        """Begin a mark for whichever tool `self._bar.active_tool` names,
        at `pos` (this widget's own window coordinates -- the same space
        `_marks` lives in). Only ever reached from `mousePressEvent` for a
        press that missed every resize handle, landed inside the
        selection, and found the eraser disarmed.

        docs/design/overlay-redesign.md's "Drawing" is the spec for what
        each tool does here: pen/highlighter/arrow/rect/blur arm
        `_in_progress_shape` for `_extend_stroke`/`mouseReleaseEvent` to
        grow and commit; step commits a `StepMarker` immediately, on the
        press alone, per its own "click only" entry; text opens its label
        editor instead of arming a drag (`_start_text_entry`), since that
        tool's whole gesture is the click too. A `tool` of `None` (nothing
        picked in the bar yet, or the eraser -- handled by the caller
        before this is ever reached) or any other unrecognised string is a
        no-op, mirroring editor.py's own `Canvas._new_in_progress_shape`
        guard on `self._tool is None`.
        """
        tool = self._bar.active_tool
        colour = QColor(self._ink_colour)

        if tool in _FREEHAND_MARK_CLASSES:
            shape_class = _FREEHAND_MARK_CLASSES[tool]
            self._in_progress_shape = shape_class(
                colour=colour, stroke_width=self._stroke_width, points=[pos]
            )
        elif tool in _TWO_POINT_MARK_CLASSES:
            shape_class = _TWO_POINT_MARK_CLASSES[tool]
            self._in_progress_shape = shape_class(
                colour=colour, stroke_width=self._stroke_width, start=pos, end=pos
            )
        elif tool == "blur":
            shape_class = Blur if self._blur_mode == "blur" else Pixelate
            self._in_progress_shape = shape_class(
                colour=colour,
                stroke_width=self._stroke_width,
                start=pos,
                end=pos,
                strength=self._blur_strength,
            )
        elif tool == "step":
            # Click only -- no drag, no `_in_progress_shape`, per the spec.
            self.add_mark(
                StepMarker(
                    colour=colour,
                    stroke_width=self._stroke_width,
                    point=pos,
                    number=next_step_number(self._marks),
                )
            )
            return
        elif tool == "text":
            self._start_text_entry(pos, colour)
            return
        else:
            return

        self.update()  # something to show from the very first pixel

    def _extend_stroke(self, pos: QPointF) -> None:
        """Grow `_in_progress_shape` to `pos`: append a point for a
        freehand stroke (pen/highlighter), or move its `end` for a
        two-point one (arrow/rect/blur) -- the same split editor.py's
        `Canvas.mouseMoveEvent` makes, since both classes of shape share
        the same field names (see shapes.py's `_transformed` docstring).
        """
        if isinstance(self._in_progress_shape, (Pen, Highlighter)):
            self._in_progress_shape.points.append(pos)
        else:
            self._in_progress_shape.end = pos
        self.update()

    # -- text tool (SNX-52) -------------------------------------------------

    def _start_text_entry(self, pos: QPointF, colour: QColor) -> None:
        """Open the text tool's label editor at `pos`, seeded with a
        placeholder and focused for immediate typing, per
        docs/design/overlay-redesign.md's "Drawing": "text -- click drops
        an editable label seeded with Label, focused for immediate
        typing." Mirrors editor.py's `Canvas.mousePressEvent` handling of
        `Tool.TEXT` -- the label commits later, via `_commit_text`, never
        here.
        """
        self._pending_text_point = pos
        self._pending_text_colour = colour
        self._pending_text_stroke_width = self._stroke_width
        text_edit = self._ensure_text_edit()
        text_edit.clear()
        text_edit.move(pos.toPoint())
        text_edit.show()
        text_edit.setFocus()

    def _ensure_text_edit(self) -> QLineEdit:
        if self._text_edit is None:
            self._text_edit = QLineEdit(self)
            # Grey hint text, not a seeded value -- see `_commit_text`'s
            # `if self._text_edit.text():` guard, same reasoning as
            # editor.py's `Canvas._ensure_text_edit`.
            self._text_edit.setPlaceholderText("Label")
            self._text_edit.hide()
            self._text_edit.editingFinished.connect(self._commit_text)
        return self._text_edit

    def _commit_text(self) -> None:
        # Re-entrancy guard, not a signal disconnect: hide() below drops
        # the field's focus and re-triggers editingFinished synchronously
        # -- see Canvas._commit_text's own docstring for the identical
        # reasoning this mirrors.
        if self._committing_text:
            return
        self._committing_text = True
        try:
            if self._text_edit.text():
                self.add_mark(
                    Text(
                        colour=self._pending_text_colour,
                        stroke_width=self._pending_text_stroke_width,
                        point=self._pending_text_point,
                        text=self._text_edit.text(),
                    )
                )
            self._text_edit.hide()
            self._pending_text_point = None
        finally:
            self._committing_text = False

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        # SNX-48: tracked on every move regardless of mode, so
        # `_select_full_screen` always has a recent position to answer
        # "which display is the cursor on" from -- see its own docstring
        # for why this beats `QCursor.pos()`.
        self._cursor_pos = event.position()

        if self._picking_window:
            # Live preview while armed: a hit sets `_selection` to that
            # window's rect, a miss clears it -- mirrors `Overlay`'s own
            # "a miss actively clears any previously-shown preview instead
            # of leaving it stuck." None of the resize/stroke/cursor logic
            # below applies while picking, so this returns unconditionally.
            rect = self._geometry_provider.window_at(self._to_absolute(event.position()))
            self.set_selection(self._to_local_rect(rect).toRect() if rect is not None else None)
            self.setCursor(Qt.CursorShape.CrossCursor)
            super().mouseMoveEvent(event)
            return

        if self._picking_freeform:
            # Same shape as the Window branch above: while a lasso is being
            # traced, none of the resize/stroke/cursor logic below applies,
            # so this returns unconditionally. Before the first press (drag
            # not yet started), there is nothing to extend -- a plain hover
            # over the frozen desktop while armed, same as `Overlay`'s own
            # freeform mode shows no live preview until a press begins one.
            if self._freeform_drag_path is not None:
                self._extend_freeform_drag(event.position())
            self.setCursor(Qt.CursorShape.CrossCursor)
            super().mouseMoveEvent(event)
            return

        if self._region_drag_anchor is not None:
            # SNX-57: same "handled here, nothing else runs" shape the
            # Window/Freeform branches above already use for their own
            # in-progress picks -- a rectangle drag-to-create is never also
            # a resize or a stroke while it's live.
            # QRectF's two-point constructor, not QRect's -- QRect(p1, p2)
            # treats both points as inclusive corners and would report one
            # pixel more of width/height than the cursor has actually
            # travelled. Mirrors `Overlay`'s own RECTANGLE-mode drag above
            # (`QRectF(self._drag_anchor, absolute_pos).normalized()`).
            rect = QRectF(self._region_drag_anchor, event.position()).normalized()
            self.set_selection(rect.toRect())
            self.setCursor(Qt.CursorShape.CrossCursor)
            super().mouseMoveEvent(event)
            return

        if self._in_progress_shape is not None:
            self._extend_stroke(event.position())
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
        if self._picking_freeform and self._freeform_drag_path is not None:
            # A release with a lasso actually in progress always confirms
            # or discards it, never falls through to the resize/stroke
            # logic below -- same "stop event propagation" rule the press
            # handler above already follows for this mode.
            self._confirm_freeform_pick(event.position())
            return
        if self._region_drag_anchor is not None:
            # SNX-57: same "stop event propagation" rule as Freeform above
            # -- a release with a Region drag-to-create in progress always
            # commits or discards it, never falls through to the resize/
            # stroke logic below.
            self._confirm_region_drag(event.position())
            return
        self._active_handle = None
        self._resize_anchor = None
        if self._in_progress_shape is not None:
            shape = self._in_progress_shape
            self._in_progress_shape = None
            committed = finalize_mark(shape)
            if committed is not None:
                self.add_mark(committed)
            else:
                # Below the spec's minimum size -- discarded, not
                # committed (shapes.finalize_mark's own docstring is the
                # authority for which shapes/thresholds that covers). Still
                # needs a repaint: `_paint_marks` was showing this shape's
                # live preview up to the instant of release.
                self.update()

    def _confirm_region_drag(self, pos: QPointF) -> None:
        """End a Region-mode drag-to-create at `pos` (window coordinates)
        and either commit or discard it. Only ever reached from
        `mouseReleaseEvent` while `_region_drag_anchor` is set.

        Discards below `tokens.Metric.SEL_MIN_W/H` -- the same floor
        `_resize_selection` already clamps re-framing to -- rather than
        committing an unusable sliver, per the ticket's own acceptance
        criterion; a plain click (no movement at all) is already well
        under that floor, so no separate misfire check is needed on top
        of it.
        """
        anchor = self._region_drag_anchor
        self._region_drag_anchor = None
        # QRectF's two-point constructor, not QRect's -- see
        # `mouseMoveEvent`'s own comment above on why the inclusive-corner
        # one would over-report by a pixel on each axis.
        rect = QRectF(anchor, pos).normalized().toRect()
        metric = design.tokens.Metric
        if rect.width() < metric.SEL_MIN_W or rect.height() < metric.SEL_MIN_H:
            self.set_selection(None)
            return
        self.set_selection(rect)

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
            # Chips (SNX-43): last in the "selection" layer's paint order,
            # per the README's "undimmed pixmap, ink layer, frame stroke,
            # handles, chips" -- and never part of `rendered_image()`'s own
            # export path, which flattens `_marks` onto the frame directly
            # and never calls this method, so neither chip can leak into a
            # save/copy.
            self._paint_dimension_chip(painter)
            self._paint_frozen_pill(painter)
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
        if not self._marks and self._in_progress_shape is None:
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
                # shapes.ObscuringShape.draw()) -- compositing a committed
                # one live against a resizable selection, on every repaint,
                # is a later ticket's concern; render_selection() (export)
                # still handles it correctly, via render()'s own
                # special-casing. An *in-progress* blur/pixelate drag gets
                # a lightweight marquee instead -- see
                # `_paint_in_progress_shape` below -- so the tool this
                # ticket wires up isn't silently invisible while dragging.
                continue
            shape.draw(painter)
        if self._in_progress_shape is not None:
            self._paint_in_progress_shape(painter, self._in_progress_shape)
        painter.restore()

    def _paint_in_progress_shape(self, painter: QPainter, shape: Shape) -> None:
        """Live preview of the mark `_start_stroke`/`_extend_stroke` are
        currently building -- everything but `StepMarker`/`Text`, which
        commit on press alone and never reach `_in_progress_shape` (see
        `_start_stroke`).

        `Rectangle`/`Arrow.draw()` already tolerate either corner order
        (see their own docstrings, "for the live in-progress preview"), so
        the shape's own `draw()` is enough for every tool but blur/
        pixelate: an `ObscuringShape` has no live preview of its own --
        `_paint_marks` above never draws a *committed* one either, for the
        same cost reason -- so this borrows `shapes.Crop`'s dashed-outline
        marquee instead, built from this shape's own colour/stroke/corners
        rather than the real (expensive, ordering-dependent) blur effect.
        """
        if isinstance(shape, ObscuringShape):
            Crop(
                colour=shape.colour,
                stroke_width=shape.stroke_width,
                start=shape.start,
                end=shape.end,
            ).draw(painter)
            return
        shape.draw(painter)

    def _paint_scrim(self, painter: QPainter) -> None:
        """Layer 2: dim everything outside the selection.

        Painted here, in this widget's own paintEvent, rather than as a
        translucent child widget stacked over the whole window -- per the
        spec, a full-window child would sit above the (future) ink layer in
        z-order and eat its mouse events. A single even-odd fill dims the
        window and punches the selection out in one call, so there's no
        separate "dim then punch a hole" step that could disagree with this
        one at the selection's edge.

        SNX-49: while a Freeform lasso is being traced or has just been
        confirmed, the hole this punches is the lasso's own outline rather
        than its bounding rect, per docs/design/overlay-redesign.md's
        "Capture modes" entry for Freeform ("the dim scrim inverts against
        it"). The selection frame/handles/chips/bar painted after this all
        still key off `_selection` (the path's own bounding rect) -- only
        this hole follows the path's exact shape.
        """
        widget_rect = QRectF(self.rect())
        path = QPainterPath()
        path.addRect(widget_rect)
        lasso = (
            self._freeform_drag_path
            if self._freeform_drag_path is not None
            else self._selection_path
        )
        if lasso is not None:
            path.addPath(lasso)
        elif self._selection is not None:
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

    # -- chips above the selection (SNX-43) ---------------------------------

    def _chip_font(self, spec: tuple[float, int], family: str) -> QFont:
        """A QFont at `spec`'s (pixel size, weight) in `family`.

        Built fresh rather than cached: both the rect-computing helpers
        below and their paint counterparts need one, and building it twice
        from the same `tokens.Font` entry is what keeps the size a rect was
        measured against and the size actually painted from ever drifting
        apart -- there's no cached QFont either could go stale against.
        """
        size, weight = spec
        font = QFont(family)
        font.setPixelSize(round(size))
        font.setWeight(QFont.Weight(weight))
        return font

    def _dimension_chip_texts(self) -> tuple[str, str]:
        """(`"1040 × 560"`, `"2 marks"`/`"1 mark"`) for the dimension chip.

        Recomputed from live state on every call -- never cached -- which is
        what makes the chip "update live while the selection is being
        resized" (the acceptance criterion): paintEvent calls this fresh on
        every repaint, and a resize drag, add_mark, undo, redo, clear or
        erase_at all end in `self.update()`, so the next paint always reads
        the current selection size and mark count. The mark count is
        singular at exactly one and plural otherwise, per the README's
        "1 mark" / "2 marks".

        Width/height come from `self._selection`, already this widget's own
        window-local *logical* pixels (see the class docstring) -- never
        `self._frame.image`'s larger pixel size. That's what keeps this
        correct on a display with device pixel ratio above one, per the
        README's HiDPI note: "the dimension chip should report logical
        selection size."
        """
        width = round(self._selection.width())
        height = round(self._selection.height())
        count = len(self._marks)
        unit = "mark" if count == 1 else "marks"
        return f"{width} × {height}", f"{count} {unit}"

    def _dimension_chip_rect(self) -> QRectF:
        """Bounding rect of the dimension chip, in window coordinates:
        left-aligned to the selection's left edge, its own top edge
        `CHIP_OFFSET_Y` above the selection's top edge -- per the README's
        "left:0; top:-38px" -- sized to fit its current text exactly, the
        same way `_bracket_path`/`_edge_handle_rect` above expose their
        geometry separately from painting it.
        """
        metric = design.tokens.Metric
        sel = QRectF(self._selection)
        size_text, mark_text = self._dimension_chip_texts()
        mono = design.font_families().mono
        size_fm = QFontMetricsF(self._chip_font(design.tokens.Font.DIM_CHIP, mono))
        mute_fm = QFontMetricsF(self._chip_font(design.tokens.Font.DIM_CHIP_MUTE, mono))

        content_width = (
            size_fm.horizontalAdvance(size_text)
            + self._CHIP_INNER_GAP
            + mute_fm.horizontalAdvance(self._CHIP_DOT)
            + self._CHIP_INNER_GAP
            + mute_fm.horizontalAdvance(mark_text)
        )
        content_height = max(size_fm.height(), mute_fm.height())
        width = content_width + 2 * self._CHIP_PAD_H
        height = content_height + 2 * self._CHIP_PAD_V
        return QRectF(sel.left(), sel.top() - metric.CHIP_OFFSET_Y, width, height)

    def _frozen_pill_rect(self) -> QRectF:
        """Bounding rect of the Frozen pill, in window coordinates:
        right-aligned to the selection's right edge, its own top edge
        `CHIP_OFFSET_Y` above the selection's top edge -- per the README's
        "right:0; top:-38px" -- sized to fit the pin icon and its label.
        """
        metric = design.tokens.Metric
        sel = QRectF(self._selection)
        ui = design.font_families().ui
        fm = QFontMetricsF(self._chip_font(design.tokens.Font.FROZEN, ui))

        content_width = (
            self._FROZEN_ICON_SIZE
            + self._FROZEN_INNER_GAP
            + fm.horizontalAdvance(self._FROZEN_LABEL)
        )
        content_height = max(self._FROZEN_ICON_SIZE, fm.height())
        width = content_width + 2 * self._CHIP_PAD_H
        height = content_height + 2 * self._CHIP_PAD_V
        top = sel.top() - metric.CHIP_OFFSET_Y
        return QRectF(sel.right() - width, top, width, height)

    def _paint_dimension_chip(self, painter: QPainter) -> None:
        """The left-hand chip: `WIDTH × HEIGHT`, a muted middot, then the
        mark count -- singular at one, per docs/design/overlay-redesign.md's
        "Chips above the selection".
        """
        mono = design.font_families().mono
        size_text, mark_text = self._dimension_chip_texts()
        size_font = self._chip_font(design.tokens.Font.DIM_CHIP, mono)
        mute_font = self._chip_font(design.tokens.Font.DIM_CHIP_MUTE, mono)
        size_fm = QFontMetricsF(size_font)
        mute_fm = QFontMetricsF(mute_font)

        rect = self._dimension_chip_rect()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(design.color("CHIP_LIGHT_BG"))
        painter.drawRoundedRect(rect, self._CHIP_RADIUS, self._CHIP_RADIUS)

        # Both fonts share the same 12px pixel size (only the weight
        # differs), so a single baseline -- derived from the size text's own
        # ascent -- keeps every segment sitting on the same line rather than
        # each drawText computing (and risking disagreeing on) its own.
        baseline = rect.top() + self._CHIP_PAD_V + size_fm.ascent()
        x = rect.left() + self._CHIP_PAD_H

        painter.setFont(size_font)
        painter.setPen(design.color("CHIP_LIGHT_FG"))
        painter.drawText(QPointF(x, baseline), size_text)
        x += size_fm.horizontalAdvance(size_text) + self._CHIP_INNER_GAP

        painter.setFont(mute_font)
        painter.setPen(design.color("CHIP_DOT"))
        painter.drawText(QPointF(x, baseline), self._CHIP_DOT)
        x += mute_fm.horizontalAdvance(self._CHIP_DOT) + self._CHIP_INNER_GAP

        painter.setPen(design.color("CHIP_LIGHT_MUTE"))
        painter.drawText(QPointF(x, baseline), mark_text)

    def _paint_frozen_pill(self, painter: QPainter) -> None:
        """The right-hand pill: a pin glyph then the word "Frozen", telling
        the user the desktop behind the overlay is a still frame, not the
        live screen -- docs/design/overlay-redesign.md's "Overlay window"
        section.
        """
        ui = design.font_families().ui
        font = self._chip_font(design.tokens.Font.FROZEN, ui)
        fm = QFontMetricsF(font)

        rect = self._frozen_pill_rect()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(design.color("CHIP_DARK_BG"))
        painter.drawRoundedRect(rect, self._CHIP_RADIUS, self._CHIP_RADIUS)

        icon_size = self._FROZEN_ICON_SIZE
        icon_x = rect.left() + self._CHIP_PAD_H
        icon_y = rect.top() + (rect.height() - icon_size) / 2
        pixmap = design.icon("pin", design.color("CHIP_DARK_FG")).pixmap(icon_size, icon_size)
        painter.drawPixmap(QPointF(icon_x, icon_y), pixmap)

        text_x = icon_x + icon_size + self._FROZEN_INNER_GAP
        baseline = rect.top() + (rect.height() - fm.height()) / 2 + fm.ascent()
        painter.setFont(font)
        painter.setPen(design.color("CHIP_DARK_FG"))
        painter.drawText(QPointF(text_x, baseline), self._FROZEN_LABEL)


class _MonitorVeil(QWidget):
    """SNX-58: the non-interactive Wayland companion `open_overlay` shows
    on every monitor besides the one the real, interactive `OverlayWindow`
    covers.

    A Wayland client cannot span two outputs with one surface -- a
    fullscreen request is inherently a single `wl_output`'s, per
    `OverlayWindow.show_on_screen`'s own docstring -- so covering a
    multi-monitor virtual desktop there takes one fullscreen surface per
    monitor rather than one big window the way X11's single `OverlayWindow`
    already can. This is deliberately not another `OverlayWindow`: only one
    monitor is ever the interactive one for a given snip, so the rest just
    need their own frozen, dimmed pixels and nothing else -- no bar, no
    tray, no selection of their own. It never seeks focus or reacts to
    input; `open_overlay` closes every instance of this the moment the
    real `OverlayWindow` does, via that window's own `on_dismissed`.
    """

    def __init__(self, monitor_frame: Frame, parent=None):
        super().__init__(parent)
        self._image = monitor_frame.image
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        # Sized to this monitor alone, exactly like `Overlay` above --
        # `show_on_screen` below is what fullscreens it onto the matching
        # real `QScreen`; this resize is what its own paintEvent's
        # `self.rect()` reads while unscreened (e.g. under the offscreen
        # platform tests run with).
        self.resize(
            round(monitor_frame.logical_size.width()),
            round(monitor_frame.logical_size.height()),
        )

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.drawImage(QRectF(self.rect()), self._image)
        # The same flat scrim `Overlay.VEIL_COLOR` dims an unselected
        # monitor with -- reused rather than re-typed, since this widget
        # never punches a selection hole in it the way `Overlay` does.
        painter.fillRect(QRectF(self.rect()), Overlay.VEIL_COLOR)
        painter.end()

    def show_on_screen(self, screen: QScreen | None) -> None:
        """Same contract as `OverlayWindow.show_on_screen` -- see its
        docstring; this window is only ever shown from the Wayland branch
        of `open_overlay`, so unlike that method there is no plain-`show()`
        case to fall back to other than a missing `screen` itself.
        """
        if screen is not None:
            self.winId()
            handle = self.windowHandle()
            if handle is not None:
                handle.setScreen(screen)
        self.showFullScreen()


def _screen_for_geometry(geometry: QRectF) -> QScreen | None:
    """The real `QScreen` whose own geometry matches `geometry` exactly, or
    None if none does -- e.g. under the offscreen platform tests run with,
    which reports no real screens a synthetic `monitor_geometries` entry
    could ever match. `OverlayWindow.show_on_screen`/`_MonitorVeil.
    show_on_screen` both already treat a None screen as "nothing more
    specific to target."
    """
    for screen in QApplication.screens():
        if QRectF(screen.geometry()) == geometry:
            return screen
    return None


def open_overlay(
    frame: Frame,
    monitor_geometries: list[QRectF],
    *,
    wayland: bool,
    hints_enabled: bool = True,
    geometry_provider: GeometryProvider | None = None,
    registry: BackendRegistry | None = None,
    on_dismissed: Callable[[], None] | None = None,
) -> OverlayWindow:
    """Build and show the overlay for one snip, positioned for the
    caller's already-detected session type (`wayland`) rather than assumed
    here, per CLAUDE.md. Returns the single interactive `OverlayWindow` --
    the only widget a caller (`app.py`) needs to keep a reference to; any
    `_MonitorVeil` companions this creates are owned by a closure wired
    through `OverlayWindow`'s own `on_dismissed` and close themselves the
    moment the returned window does, so a caller's bookkeeping never has
    to know they exist.

    `on_dismissed` (SNX-62) is the caller's own hook for that same moment
    -- composed with the veil-closing closure below rather than handed to
    `OverlayWindow` in its place, so a caller (`AppController`, to drop its
    stale `_overlay` reference the instant the session actually ends)
    doesn't have to know whether this particular snip has veils to close at
    all.

    X11 (`wayland=False`): unchanged from before this ticket -- one
    `OverlayWindow` sized to the whole virtual desktop (every entry in
    `monitor_geometries`), shown via `show_on_screen(None)`, which is
    exactly the plain `setGeometry`-then-`show()` this window already did.

    Wayland: `OverlayWindow.show_on_screen`'s docstring is the authority
    for why a single window is fullscreened onto one specific `QScreen`
    instead. Fullscreen is inherently one output at a time, so with more
    than one monitor the interactive `OverlayWindow` is cropped
    (`Frame.crop`, the same helper `Overlay` above already uses) to just
    the first monitor, and a non-interactive `_MonitorVeil` -- cropped and
    fullscreened the same way -- covers each of the rest, so every
    window's own local (0, 0) lines up with the real screen pixels under
    it and none of them paint a stretched or offset copy of another
    monitor's content.
    """
    primary_geometry = (
        monitor_geometries[0]
        if monitor_geometries
        else QRectF(frame.logical_origin, frame.logical_size)
    )

    veils: list[_MonitorVeil] = []

    def _on_overlay_dismissed() -> None:
        for veil in veils:
            veil.close()
        if on_dismissed is not None:
            on_dismissed()

    multi_monitor_wayland = wayland and len(monitor_geometries) > 1
    overlay_monitor_geometries = (
        [primary_geometry] if multi_monitor_wayland else monitor_geometries
    )
    # None (not the closure above) whenever neither half of it would do
    # anything -- no veils to close *and* no caller-supplied hook -- so
    # OverlayWindow._on_dismissed stays exactly None for a plain
    # single-window session, same as before this ticket's `on_dismissed`
    # parameter existed.
    needs_dismissal_hook = multi_monitor_wayland or on_dismissed is not None
    overlay = OverlayWindow(
        frame.crop(primary_geometry) if multi_monitor_wayland else frame,
        hints_enabled=hints_enabled,
        geometry_provider=geometry_provider,
        monitor_geometries=overlay_monitor_geometries,
        registry=registry,
        on_dismissed=_on_overlay_dismissed if needs_dismissal_hook else None,
    )

    if not wayland:
        overlay.show_on_screen(None)
        return overlay

    overlay.show_on_screen(_screen_for_geometry(primary_geometry))
    for geometry in monitor_geometries[1:]:
        veil = _MonitorVeil(frame.crop(geometry))
        veil.show_on_screen(_screen_for_geometry(geometry))
        veils.append(veil)
    return overlay


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
